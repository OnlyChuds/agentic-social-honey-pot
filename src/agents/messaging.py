from __future__ import annotations

import html as html_lib
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from src.agents.base import BaseAgent
from src.agents.identity import _linkify_html_fragment, wrap_email_html
from src.agents.qa import check_disallowed_meta_text, check_placeholders
from src.llm.text import TextLLMClient
from src.models import (
    BrandingManifest,
    Channel,
    EmailStyle,
    Proposal,
    QAVerdict,
    Snapshot,
    SystemSenderAlias,
    TouchSpec,
)

MessagingMode = Literal["compose", "signal", "live_turn"]
ProposedNextAction = Literal["reply_now", "wait", "change_angle", "escalate", "back_off"]

LINKEDIN_NOTE_MAX_CHARS = 300
LIVE_TURN_MAX_CHARS = 4000
MAX_MESSAGE_ATTACHMENTS = 10
MAX_TURN_ATTACHMENTS = 5
MAX_CONVERSATION_TURNS = 10

LIVE_CHANNELS: frozenset[str] = frozenset({"phone_call", "video_call", "teams_call", "live_chat"})
PHONE_CHANNELS: frozenset[str] = frozenset(
    {"sms", "whatsapp", "signal", "imessage", "phone_call", "voicemail"}
)
LINKEDIN_CHANNELS: frozenset[str] = frozenset(
    {"linkedin_message", "linkedin_connection_request"}
)
SOCIAL_HANDLE_CHANNELS: frozenset[str] = frozenset({"x_dm", "instagram_dm", "telegram"})
CHAT_AND_LIVE_CHANNELS: frozenset[str] = frozenset(
    {
        "sms",
        "whatsapp",
        "signal",
        "telegram",
        "imessage",
        "slack",
        "teams_chat",
        "discord",
        "x_dm",
        "instagram_dm",
        "facebook_messenger",
        "phone_call",
        "video_call",
        "teams_call",
        "live_chat",
    }
)
SUBJECT_REQUIRED_CHANNELS: frozenset[str] = frozenset({"email", "calendar_invite"})

_CLICHE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "generic opener",
        re.compile(r"\bi\s+hope\s+this\s+message\s+finds\s+you\s+well\b", re.IGNORECASE),
    ),
    ("corporate filler", re.compile(r"\bcirculate\s+back\b", re.IGNORECASE)),
    ("corporate filler", re.compile(r"\bper\s+my\s+last\b", re.IGNORECASE)),
    ("corporate filler", re.compile(r"\bat\s+your\s+earliest\s+convenience\b", re.IGNORECASE)),
    ("corporate filler", re.compile(r"\btouch\s+base\b", re.IGNORECASE)),
    ("stilted phrasing", re.compile(r"\bkindly\b", re.IGNORECASE)),
    ("assistant exclamation", re.compile(r"\b(?:certainly|absolutely)!", re.IGNORECASE)),
)
_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+\.)\s+", re.MULTILINE)
_PHANTOM_ATTACHMENT_RE = re.compile(
    r"\b(?:see\s+attached|i(?:'ve|’ve|\s+have)?\s+attached|attached\s+(?:is|are|here|below))\b",
    re.IGNORECASE,
)
_SUBJECT_BODY_LEAK_RE = re.compile(r"\b(?:hi|hello|hey)\s+[A-Z][a-z]+,", re.IGNORECASE)
_STACKED_RE_PREFIX_RE = re.compile(r"^(?:re:\s*){2,}", re.IGNORECASE)
_ASSET_ID_STR_RE = re.compile(r"^(?:asset://)?a_[A-Za-z0-9_-]+$")
SUBJECT_MAX_CHARS = 90


class StoryClock:
    """Monotonic story-time clock generating realistic business-hour ``sent_at`` timestamps.

    Target API rejects any ``sent_at`` earlier than the previous contact in a session (HTTP 409)
    and defaults to German time (``Europe/Berlin``) when no offset is supplied.
    """

    def __init__(
        self,
        start: datetime | None = None,
        *,
        timezone: str = "Europe/Berlin",
        work_start_hour: int = 9,
        work_end_hour: int = 17,
    ) -> None:
        self.tz = ZoneInfo(timezone)
        self.work_start_hour = work_start_hour
        self.work_end_hour = work_end_hour
        base = start.astimezone(self.tz) if start is not None else datetime.now(self.tz)
        self._current = self._clamp_business_hours(base)

    def _clamp_business_hours(self, dt: datetime) -> datetime:
        if dt.hour < self.work_start_hour:
            dt = dt.replace(hour=self.work_start_hour, minute=12, second=0, microsecond=0)
        elif dt.hour >= self.work_end_hour:
            next_day = dt + timedelta(days=1)
            dt = next_day.replace(hour=self.work_start_hour, minute=15, second=0, microsecond=0)
        # Never send business outreach on Saturday (5) or Sunday (6) — roll to Monday morning
        if dt.weekday() >= 5:
            days_until_monday = 7 - dt.weekday()
            dt = (dt + timedelta(days=days_until_monday)).replace(
                hour=self.work_start_hour, minute=15, second=0, microsecond=0
            )
        return dt.replace(microsecond=0)

    @property
    def current_iso(self) -> str:
        return self._current.isoformat()

    def advance_message(self, *, minutes: int = 95) -> str:
        """Advance story time between distinct contacts while staying in business hours."""
        candidate = self._clamp_business_hours(self._current + timedelta(minutes=max(1, minutes)))
        if candidate <= self._current:
            candidate = self._current + timedelta(minutes=max(1, minutes))
        self._current = candidate
        return self._current.isoformat()

    def advance_turn(self, *, seconds: int = 25) -> str:
        """Advance story time slightly between turns inside an active live conversation."""
        self._current = (self._current + timedelta(seconds=max(1, seconds))).replace(microsecond=0)
        return self._current.isoformat()


class SenderAddress(BaseModel):
    name: str = ""
    address: str = ""


class MessageDraft(BaseModel):
    mode: Literal["compose"] = "compose"
    channel: Channel
    subject: str | None = None
    body: str
    attachments: list[str | dict[str, Any]] = Field(default_factory=list)
    page_request: str | None = None
    facts_used: list[str] = Field(default_factory=list)
    from_sender: SenderAddress | None = None
    sent_at: str | None = None
    hang_up: bool = False

    def to_api_message_payload(self, target_id: str) -> dict[str, Any]:
        """Format this draft for ``POST /v1/sessions/{sid}/messages``."""
        payload: dict[str, Any] = {
            "to": target_id,
            "channel": self.channel,
            "body": self.body,
        }
        if self.from_sender is not None:
            sender_obj: dict[str, str] = {}
            if self.from_sender.name:
                sender_obj["name"] = self.from_sender.name
            if self.from_sender.address:
                sender_obj["address"] = self.from_sender.address
            if sender_obj:
                payload["from"] = sender_obj
        if self.subject:
            payload["subject"] = self.subject
        if self.attachments:
            payload["attachments"] = list(self.attachments[:MAX_MESSAGE_ATTACHMENTS])
        if self.sent_at:
            payload["sent_at"] = self.sent_at
        return payload

    def to_api_turn_payload(self) -> dict[str, Any]:
        """Format this draft for ``POST /v1/conversations/{cid}/turns``."""
        payload: dict[str, Any] = {
            "say": _trim_at_word_boundary(self.body.strip(), LIVE_TURN_MAX_CHARS)
        }
        if self.attachments:
            payload["attachments"] = list(self.attachments[:MAX_TURN_ATTACHMENTS])
        if self.sent_at:
            payload["sent_at"] = self.sent_at
        return payload


class ConversationTurnDraft(BaseModel):
    channel: Channel
    say: str
    attachments: list[str | dict[str, Any]] = Field(default_factory=list)
    facts_used: list[str] = Field(default_factory=list)
    from_sender: SenderAddress | None = None
    sent_at: str | None = None
    hang_up: bool = False

    def to_api_payload(self) -> dict[str, Any]:
        """Return exact JSON body for ``POST /v1/conversations/{cid}/turns``."""
        payload: dict[str, Any] = {
            "say": _trim_at_word_boundary(self.say.strip(), LIVE_TURN_MAX_CHARS)
        }
        if self.attachments:
            payload["attachments"] = list(self.attachments[:MAX_TURN_ATTACHMENTS])
        if self.sent_at:
            payload["sent_at"] = self.sent_at
        return payload


class SignalAssessment(BaseModel):
    mode: Literal["signal"] = "signal"
    engagement: float = Field(ge=0.0, le=1.0)
    suspicion: float = Field(ge=0.0, le=1.0)
    hook_hits: list[str] = Field(default_factory=list)
    mood: str = "neutral"
    proposed_next: ProposedNextAction = "wait"
    reason: str = ""


_HTML_STRIP_BLOCKS_RE = re.compile(
    r"<(head|style|script|svg)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_HTML_ANCHOR_RE = re.compile(
    r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html_for_context(text: str, *, max_chars: int = 800) -> str:
    """Convert wrapped HTML email bodies to compact readable text for LLM history context."""
    if not text or "<" not in text:
        return text[:max_chars]
    cleaned = _HTML_STRIP_BLOCKS_RE.sub(" ", text)
    cleaned = _HTML_ANCHOR_RE.sub(lambda m: f"{m.group(2).strip()} ({m.group(1)})", cleaned)
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    cleaned = html_lib.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_chars]


def _compact_conversation_for_llm(snapshot: Snapshot) -> list[dict[str, Any]]:
    """Return recent conversation turns with HTML wrapper boilerplate stripped."""
    turns: list[dict[str, Any]] = []
    for turn in snapshot.conversation.turns[-12:]:
        item = turn.model_dump(mode="json")
        if isinstance(item.get("body"), str):
            item["body"] = _strip_html_for_context(item["body"])
        turns.append(item)
    return turns


def _compact_manifest_for_llm(manifest: BrandingManifest | None) -> dict[str, Any] | None:
    """Return persona/voice fields of BrandingManifest without SVG/palette bloat."""
    if manifest is None:
        return None
    data = manifest.model_dump(mode="json")
    data.pop("logo_svg", None)
    data.pop("palette", None)
    data.pop("fonts", None)
    return data


def _trim_at_word_boundary(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    truncated = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    if not truncated:
        truncated = text[: limit - 1]
    return f"{truncated}."


def _derive_social_handle(sender_name: str, fallback: str = "") -> str:
    tokens = re.findall(r"[a-z0-9]+", sender_name.lower())
    if tokens:
        return "@" + "_".join(tokens[:2])
    return fallback


def _find_system_sender(
    manifest: BrandingManifest,
    alias: str | None,
    *,
    fallback_keywords: tuple[str, ...] = (),
) -> SystemSenderAlias | None:
    if not manifest.system_senders:
        return None
    if alias:
        needle = alias.strip().lower()
        for item in manifest.system_senders:
            if item.alias.strip().lower() == needle:
                return item
    if fallback_keywords:
        for item in manifest.system_senders:
            low = item.alias.strip().lower()
            if any(kw in low for kw in fallback_keywords):
                return item
    return None


def resolve_sender_for_channel(
    manifest: BrandingManifest | None,
    channel: Channel,
    *,
    sender_alias: str | None = None,
) -> SenderAddress | None:
    """Resolve the Target API ``from`` object across all 22 channels from ``BrandingManifest``."""
    if manifest is None:
        return None

    alias_entry = _find_system_sender(manifest, sender_alias)
    if alias_entry is not None:
        name = alias_entry.display_name.strip() or manifest.sender_name or manifest.company
        if channel in PHONE_CHANNELS:
            address = alias_entry.phone.strip() or manifest.phone or alias_entry.email.strip() or manifest.email
        else:
            address = alias_entry.email.strip() or manifest.email
        if name or address:
            return SenderAddress(name=name, address=address)

    if channel == "mfa_push":
        sys_mfa = _find_system_sender(
            manifest,
            None,
            fallback_keywords=("mfa", "security", "sso", "auth", "it", "noreply"),
        )
        if sys_mfa is not None:
            name = sys_mfa.display_name.strip() or f"{manifest.company or manifest.sender_name} Security"
            address = sys_mfa.email.strip() or (f"security@{manifest.domain}" if manifest.domain else manifest.email)
            return SenderAddress(name=name, address=address)
        org_label = manifest.company.strip() or manifest.sender_name.strip() or "IT"
        address = f"security@{manifest.domain.strip()}" if manifest.domain.strip() else manifest.email
        return SenderAddress(name=f"{org_label} Security", address=address)

    if channel in LINKEDIN_CHANNELS:
        headline_parts = [p for p in (manifest.title.strip(), manifest.company.strip()) if p]
        headline = " at ".join(headline_parts) if len(headline_parts) == 2 else "".join(headline_parts)
        display = f"{manifest.sender_name.strip()} — {headline}" if (manifest.sender_name.strip() and headline) else manifest.sender_name.strip()
        if not display and not manifest.email:
            return None
        return SenderAddress(name=display, address=manifest.email)

    if channel in SOCIAL_HANDLE_CHANNELS:
        handle = _derive_social_handle(manifest.sender_name, fallback=manifest.email)
        if not manifest.sender_name and not handle:
            return None
        return SenderAddress(name=manifest.sender_name, address=handle)

    if channel in PHONE_CHANNELS:
        address = manifest.phone if manifest.phone else manifest.email
        if not manifest.sender_name and not address:
            return None
        return SenderAddress(name=manifest.sender_name, address=address)

    # email, calendar_invite, shared_document, slack, teams_chat, teams_call,
    # video_call, discord, facebook_messenger, live_chat
    address = manifest.email
    if not manifest.sender_name and not address:
        return None
    return SenderAddress(name=manifest.sender_name, address=address)


def validate_facts_used(
    facts_used: Sequence[str],
    snapshot: Snapshot,
    *,
    is_first_contact: bool = False,
) -> tuple[list[str], list[str]]:
    """Filter ``facts_used`` to active snapshot facts and report referential/confidence warnings."""
    active_by_id = {f.fact_id: f for f in snapshot.facts if f.status == "active"}
    valid: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()

    for fid in facts_used:
        if fid in seen:
            continue
        seen.add(fid)
        fact = active_by_id.get(fid)
        if fact is None:
            warnings.append(f"Dropped unknown or superseded fact_id '{fid}'.")
            continue
        if is_first_contact and fact.provenance == "inferred" and fact.confidence < 0.5:
            warnings.append(
                f"Dropped low-confidence inferred fact '{fid}' "
                f"(confidence={fact.confidence:.2f}) disallowed on first contact."
            )
            continue
        valid.append(fid)

    return valid, warnings


def enforce_channel_limits(draft: MessageDraft) -> tuple[MessageDraft, list[str]]:
    """Enforce hard Target API and channel-specific format constraints on a MessageDraft."""
    warnings: list[str] = []
    updated = draft.model_copy(deep=True)

    if (
        updated.channel == "linkedin_connection_request"
        and len(updated.body) > LINKEDIN_NOTE_MAX_CHARS
    ):
        updated.body = _trim_at_word_boundary(updated.body, LINKEDIN_NOTE_MAX_CHARS)
        warnings.append(
            f"Trimmed linkedin_connection_request body to <= {LINKEDIN_NOTE_MAX_CHARS} chars."
        )

    if updated.channel in LIVE_CHANNELS and len(updated.body) > LIVE_TURN_MAX_CHARS:
        updated.body = _trim_at_word_boundary(updated.body, LIVE_TURN_MAX_CHARS)
        warnings.append(f"Trimmed live turn body to <= {LIVE_TURN_MAX_CHARS} chars.")

    if updated.subject is not None:
        cleaned_subj = re.sub(r"\s+", " ", updated.subject).strip()
        cleaned_subj = _STACKED_RE_PREFIX_RE.sub("Re: ", cleaned_subj)
        leak_match = _SUBJECT_BODY_LEAK_RE.search(cleaned_subj)
        if leak_match and leak_match.start() > 0:
            cleaned_subj = cleaned_subj[: leak_match.start()].strip(" -:;,")
            warnings.append("Stripped leaked greeting/body text from email subject.")
        if len(cleaned_subj) > SUBJECT_MAX_CHARS:
            cleaned_subj = _trim_at_word_boundary(cleaned_subj, SUBJECT_MAX_CHARS).rstrip(".")
            warnings.append(f"Trimmed email subject to <= {SUBJECT_MAX_CHARS} chars.")
        updated.subject = cleaned_subj

    if updated.channel in SUBJECT_REQUIRED_CHANNELS and not (
        updated.subject and updated.subject.strip()
    ):
        updated.subject = "Quick follow-up"
        warnings.append(f"Added fallback subject for channel '{updated.channel}'.")

    # Convert any stray bare non-asset filename strings into valid virtual attachment dicts
    if updated.attachments:
        normalized_atts: list[str | dict[str, Any]] = []
        for item in updated.attachments:
            if isinstance(item, str):
                raw = item.strip()
                if _ASSET_ID_STR_RE.match(raw):
                    normalized_atts.append(raw.removeprefix("asset://"))
                elif raw:
                    normalized_atts.append({"filename": Path(raw).name})
                    warnings.append(
                        f"Converted bare attachment filename '{raw}' to virtual attachment object."
                    )
            else:
                normalized_atts.append(item)
        updated.attachments = normalized_atts

    att_limit = MAX_TURN_ATTACHMENTS if updated.channel in LIVE_CHANNELS else MAX_MESSAGE_ATTACHMENTS
    if len(updated.attachments) > att_limit:
        updated.attachments = updated.attachments[:att_limit]
        warnings.append(f"Trimmed attachments list to Target API limit of {att_limit}.")

    return updated, warnings


def lint_message_style(draft: MessageDraft) -> list[str]:
    """Run deterministic style, formatting, and rule-compliance checks on a MessageDraft."""
    issues: list[str] = []
    combined = f"{draft.subject or ''}\n{draft.body}"

    for label, pattern in _CLICHE_PATTERNS:
        match = pattern.search(combined)
        if match:
            issues.append(f"Remove {label} phrase '{match.group(0)}'.")

    if draft.channel in CHAT_AND_LIVE_CHANNELS and _BULLET_LINE_RE.search(draft.body):
        issues.append(
            f"Remove markdown/bullet list formatting from chat/live channel '{draft.channel}'."
        )

    if draft.subject:
        subj_clean = re.sub(r"\s+", " ", draft.subject).strip()
        if len(subj_clean) > 100 or _SUBJECT_BODY_LEAK_RE.search(subj_clean):
            issues.append(
                "Subject line looks corrupted or contains email body text — "
                "use a crisp 3-8 word subject line only."
            )

    if not draft.attachments and _PHANTOM_ATTACHMENT_RE.search(draft.body):
        issues.append(
            "Body claims an attachment is included, but `attachments` is empty — "
            "either include a concrete attachment or remove the 'attached' wording."
        )

    for att in draft.attachments:
        if isinstance(att, str) and not _ASSET_ID_STR_RE.match(att.strip()):
            issues.append(
                f"Bare filename string '{att}' in `attachments` is not a valid uploaded asset ID — "
                "use a virtual object `{\"filename\": ..., \"transcript\": \"...detailed contents...\"}` "
                "or omit the attachment."
            )

    for placeholder_issue in check_placeholders(combined):
        issues.append(placeholder_issue.fix)

    for meta_issue in check_disallowed_meta_text(combined):
        issues.append(meta_issue.fix)

    return issues


_ATTACHMENT_AVERSION_RE = re.compile(
    r"(?:no\s+attachments?|don'?t\s+open\s+attachments?|won'?t\s+open\s+(?:the\s+)?attachments?|"
    r"refus(?:es|ed)\s+attachments?|skeptical\s+of\s+attachments?|avoid\s+attachments?)",
    re.IGNORECASE,
)
_SELF_URL_RE = re.compile(r"https?://([a-z0-9.-]+)(/[^\s<>\"')]*)", re.IGNORECASE)


def attachments_disallowed(
    snapshot: Snapshot,
    *,
    is_first_contact: bool,
    latest_signal: SignalAssessment | dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Deterministic hard rule: block attachments when target dislikes them or trust is unestablished."""
    for watchout in snapshot.profile.watchouts:
        if _ATTACHMENT_AVERSION_RE.search(watchout):
            return True, f"profile watchout forbids attachments ({watchout})"
    for turn in snapshot.conversation.turns[-6:]:
        if turn.dir == "in" and _ATTACHMENT_AVERSION_RE.search(turn.body):
            return True, "target explicitly refused attachments in conversation"

    plan = snapshot.campaign.plan
    is_explicit_action = plan is not None and plan.objective == "action" and bool(plan.action_artifact)
    suspicion = 0.0
    if isinstance(latest_signal, SignalAssessment):
        suspicion = latest_signal.suspicion
    elif isinstance(latest_signal, dict) and latest_signal.get("suspicion") is not None:
        try:
            suspicion = float(latest_signal["suspicion"])
        except (TypeError, ValueError):
            suspicion = 0.0

    if is_first_contact and not is_explicit_action:
        return True, "no attachments on first contact before rapport is established"
    if suspicion >= 0.5:
        return True, f"suspicion {suspicion:.2f} >= 0.50 (attachments disabled until trust improves)"
    return False, ""


def _ensure_registered_url_anchored(
    body: str,
    *,
    registered_page_url: str | None,
    manifest: BrandingManifest | None,
) -> str:
    """Ensure bare URLs are clickable <a> tags and registered_page_url is anchored in HTML emails."""
    accent = (
        manifest.palette[1]
        if manifest is not None and len(manifest.palette) > 1 and manifest.palette[1].startswith("#")
        else "#2563eb"
    )
    updated = _linkify_html_fragment(body, accent)
    if registered_page_url and registered_page_url not in updated:
        cta_p = (
            '<p style="margin:14px 0 0 0;line-height:1.55;">'
            f'<a href="{html_lib.escape(registered_page_url, quote=True)}" '
            f'style="color:{accent};text-decoration:underline;font-weight:500;">'
            f"{html_lib.escape(registered_page_url)}</a></p>"
        )
        sig_idx = updated.find('<table role="presentation"')
        if sig_idx != -1:
            updated = updated[:sig_idx] + cta_p + updated[sig_idx:]
        elif "</body>" in updated.lower():
            updated = re.sub(r"</body>", f"{cta_p}</body>", updated, count=1, flags=re.IGNORECASE)
        else:
            updated = updated + cta_p
    return updated


class MessagingAgent(BaseAgent):
    """Typed multi-channel compose, live-turn, and signal-classification agent."""

    agent_name = "messaging"
    proposal_kind = "message"

    def __init__(
        self,
        llm: TextLLMClient,
        *,
        prompts_dir: Path = Path("prompts"),
    ) -> None:
        super().__init__(llm, prompts_dir=prompts_dir)

    def _next_planned_touch(self, snapshot: Snapshot) -> TouchSpec | None:
        plan = snapshot.campaign.plan
        if plan is None or not plan.touches:
            return None
        next_n = snapshot.campaign.budget.used + 1
        for touch in plan.touches:
            if touch.n == next_n:
                return touch
        return plan.touches[-1]

    async def compose(
        self,
        snapshot: Snapshot,
        *,
        touch_spec: TouchSpec | None = None,
        latest_signal: SignalAssessment | dict[str, Any] | None = None,
        registered_page_url: str | None = None,
        known_registered_urls: Sequence[str] | None = None,
        campaign_memory: dict[str, Any] | None = None,
        qa_feedback: QAVerdict | None = None,
        rejection_feedback: str | None = None,
        sent_at: str | None = None,
        current_story_time: str | None = None,
        channel_info: dict[str, Any] | None = None,
        email_style: EmailStyle | None = None,
        action_artifact: dict[str, Any] | None = None,
        objective: str | None = None,
        sender_alias: str | None = None,
        live_turn_context: dict[str, Any] | None = None,
        branded_html: bool | None = None,
        portrait_ref: str | None = None,
        auto_rewrite: bool = True,
    ) -> tuple[MessageDraft, Proposal]:
        """Compose a schema-validated outbound message proposal for the next planned touch."""
        active_touch = touch_spec or self._next_planned_touch(snapshot)
        plan = snapshot.campaign.plan
        effective_objective = objective or (plan.objective if plan is not None else None)
        effective_action_artifact = (
            action_artifact
            if action_artifact is not None
            else (plan.action_artifact if plan is not None else None)
        )
        effective_alias = sender_alias or (
            active_touch.sender_alias if active_touch is not None else None
        )
        effective_story_time = current_story_time or sent_at

        is_first_contact = snapshot.campaign.budget.used == 0 and not snapshot.conversation.turns
        no_attach, no_attach_reason = attachments_disallowed(
            snapshot,
            is_first_contact=is_first_contact,
            latest_signal=latest_signal,
        )
        allowed_self_urls = {
            u.rstrip("/").lower()
            for u in ([registered_page_url] if registered_page_url else [])
            + list(known_registered_urls or ())
            if u
        }

        ranked_hooks = sorted(
            [h for h in snapshot.hooks if h.status != "exhausted"],
            key=lambda h: (h.engagement.score, h.confidence),
            reverse=True,
        )

        context: dict[str, Any] = {
            "mode": "live_turn" if live_turn_context is not None else "compose",
            "target": snapshot.target.model_dump(mode="json"),
            "manifest": _compact_manifest_for_llm(snapshot.identity),
            "profile": snapshot.profile.model_dump(mode="json"),
            "hooks": [h.model_dump(mode="json") for h in ranked_hooks],
            "facts": [f.model_dump(mode="json") for f in snapshot.facts if f.status == "active"],
            "touch": active_touch.model_dump(mode="json") if active_touch is not None else None,
            "conversation": _compact_conversation_for_llm(snapshot),
            "registered_page_url": registered_page_url,
            "known_registered_urls": sorted(allowed_self_urls),
            "frozen_campaign_memory": campaign_memory or {},
            "attachments_disallowed": no_attach,
            "attachments_disallowed_reason": no_attach_reason if no_attach else "",
            "is_first_contact": is_first_contact,
            "objective": effective_objective,
            "current_story_time": effective_story_time,
        }
        if channel_info is not None:
            context["channel_info"] = channel_info
        if effective_action_artifact is not None:
            context["action_artifact"] = effective_action_artifact
        if email_style is not None:
            context["email_style"] = email_style
        if effective_alias is not None:
            context["sender_alias"] = effective_alias
        if live_turn_context is not None:
            context["live_turn"] = live_turn_context
        if latest_signal is not None:
            context["latest_signal"] = (
                latest_signal.model_dump(mode="json")
                if isinstance(latest_signal, BaseModel)
                else latest_signal
            )
        if qa_feedback is not None:
            context["qa_feedback"] = qa_feedback.model_dump(mode="json", by_alias=True)
        if rejection_feedback is not None:
            context["rejection_feedback"] = rejection_feedback

        def _extra_guard_issues(d: MessageDraft) -> list[str]:
            extra: list[str] = []
            if no_attach and d.attachments:
                extra.append(
                    f"Attachments are forbidden right now ({no_attach_reason}) — "
                    "set `attachments` to [] and remove any mention of attached files."
                )
            sender_dom = (
                snapshot.identity.domain.strip().lower()
                if snapshot.identity and snapshot.identity.domain
                else ""
            )
            if sender_dom:
                for m in _SELF_URL_RE.finditer(d.body):
                    host = m.group(1).lower()
                    full_u = m.group(0).rstrip(".,;:!?").rstrip("/").lower()
                    if host == sender_dom and full_u not in allowed_self_urls:
                        if registered_page_url:
                            extra.append(
                                f"Unregistered URL '{m.group(0)}' on '{sender_dom}' — "
                                f"use the exact registered URL '{registered_page_url}' instead."
                            )
                        else:
                            extra.append(
                                f"Unregistered URL '{m.group(0)}' on '{sender_dom}' — "
                                "remove invented links until a landing page is registered."
                            )
            return extra

        draft = await self.run_json(context, MessageDraft)
        lint_issues = [*lint_message_style(draft), *_extra_guard_issues(draft)]
        if lint_issues and auto_rewrite:
            retry_context = dict(context)
            retry_context["previous_draft"] = draft.model_dump(mode="json")
            retry_context["style_lint_issues"] = lint_issues
            draft = await self.run_json(retry_context, MessageDraft)
            lint_issues = [*lint_message_style(draft), *_extra_guard_issues(draft)]

        if no_attach and draft.attachments:
            draft.attachments = []
        elif (
            not no_attach
            and not draft.attachments
            and effective_objective == "action"
            and isinstance(effective_action_artifact, dict)
            and effective_action_artifact.get("filename")
            and draft.channel not in {"sms", "mfa_push", "linkedin_connection_request"}
        ):
            fname = str(effective_action_artifact["filename"]).strip()
            escalation_val = active_touch.escalation if active_touch is not None else 1
            if fname and (escalation_val >= 3 or fname.lower() in draft.body.lower()):
                att_obj: dict[str, Any] = {"filename": fname}
                if effective_action_artifact.get("transcript"):
                    att_obj["transcript"] = str(effective_action_artifact["transcript"])
                draft.attachments = [att_obj]

        draft, limit_warnings = enforce_channel_limits(draft)
        if snapshot.identity is not None and snapshot.identity.sender_name:
            draft.body = re.sub(
                r"\[(?:your|my|sender)[\s_-]?name\]",
                snapshot.identity.sender_name,
                draft.body,
                flags=re.IGNORECASE,
            )
        valid_facts, fact_warnings = validate_facts_used(
            draft.facts_used,
            snapshot,
            is_first_contact=is_first_contact,
        )
        draft.facts_used = valid_facts

        if registered_page_url is None and draft.page_request:
            fact_warnings.append(
                f"Cleared unregistered page_request '{draft.page_request}' "
                "(no landing page registered yet)."
            )
            draft.page_request = None

        if draft.from_sender is None:
            draft.from_sender = resolve_sender_for_channel(
                snapshot.identity,
                draft.channel,
                sender_alias=effective_alias,
            )
        if sent_at is not None and not draft.sent_at:
            draft.sent_at = sent_at

        chosen_email_style: EmailStyle | None = email_style or (
            active_touch.email_style if active_touch is not None else None
        )
        if chosen_email_style is None and draft.channel == "email" and snapshot.identity is not None:
            if branded_html is True:
                chosen_email_style = "branded_card"
            elif branded_html is False:
                chosen_email_style = None
            else:
                subj_lower = (draft.subject or "").strip().lower()
                is_reply_note = subj_lower.startswith("re:") or (
                    not is_first_contact
                    and effective_alias is None
                    and registered_page_url is None
                )
                if is_reply_note:
                    chosen_email_style = "personal_html"
                elif snapshot.identity.logo_svg or snapshot.identity.palette:
                    chosen_email_style = "branded_card"
                else:
                    chosen_email_style = "personal_html"

        if (
            chosen_email_style is not None
            and draft.channel == "email"
            and snapshot.identity is not None
            and "<!doctype html>" not in draft.body.lower()
        ):
            effective_portrait = portrait_ref
            if effective_portrait is None and snapshot.identity.portrait.api_asset_id:
                effective_portrait = f"asset://{snapshot.identity.portrait.api_asset_id}"
            draft.body = wrap_email_html(
                snapshot.identity,
                subject=draft.subject or "",
                body_html=draft.body,
                style=chosen_email_style,
                portrait_ref=effective_portrait,
            )

        if draft.channel == "email" and (
            "<!doctype html>" in draft.body.lower() or "<p" in draft.body.lower()
        ):
            draft.body = _ensure_registered_url_anchored(
                draft.body,
                registered_page_url=registered_page_url,
                manifest=snapshot.identity,
            )

        warnings = [*limit_warnings, *fact_warnings, *lint_issues]
        payload = draft.model_dump(mode="json")
        if chosen_email_style is not None:
            payload["email_style"] = chosen_email_style
        if warnings:
            payload["warnings"] = warnings
        if snapshot.target.id:
            payload["api_payload"] = (
                draft.to_api_turn_payload()
                if draft.channel in LIVE_CHANNELS
                else draft.to_api_message_payload(snapshot.target.id)
            )

        rationale = (
            f"Composed {draft.channel} message (facts_used={len(draft.facts_used)}"
            + (f", warnings={len(warnings)}" if warnings else "")
            + ")."
        )
        proposal = self.make_proposal(snapshot, payload, kind="message", rationale=rationale)
        return draft, proposal

    async def compose_live_turn(
        self,
        snapshot: Snapshot,
        *,
        channel: Channel = "phone_call",
        touch_spec: TouchSpec | None = None,
        latest_signal: SignalAssessment | dict[str, Any] | None = None,
        persona_says: str | None = None,
        turn_index: int = 1,
        current_story_time: str | None = None,
        registered_page_url: str | None = None,
        channel_info: dict[str, Any] | None = None,
        sender_alias: str | None = None,
        sent_at: str | None = None,
        auto_rewrite: bool = True,
    ) -> tuple[ConversationTurnDraft, Proposal]:
        """Compose a single live-conversation turn formatted for ``/conversations/{cid}/turns``."""
        if channel not in LIVE_CHANNELS:
            raise ValueError(
                f"Channel '{channel}' is not a live conversation channel "
                f"(expected one of {sorted(LIVE_CHANNELS)})."
            )

        effective_touch = touch_spec or TouchSpec(
            n=snapshot.campaign.budget.used + 1,
            channel=channel,
            intent="Respond concisely in live conversation",
            escalation=1,
        )
        if effective_touch.channel not in LIVE_CHANNELS:
            effective_touch = effective_touch.model_copy(update={"channel": channel})

        live_turn_ctx: dict[str, Any] = {
            "turn_index": turn_index,
            "max_turns": MAX_CONVERSATION_TURNS,
            "persona_says": persona_says or "",
            "instructions": (
                "Speak naturally in 1-2 short spoken sentences. Set hang_up=true only if "
                "the conversation reached a natural goodbye or clear refusal."
            ),
        }
        draft, base_proposal = await self.compose(
            snapshot,
            touch_spec=effective_touch,
            latest_signal=latest_signal,
            registered_page_url=registered_page_url,
            sent_at=sent_at,
            current_story_time=current_story_time or sent_at,
            channel_info=channel_info,
            sender_alias=sender_alias,
            live_turn_context=live_turn_ctx,
            branded_html=False,
            auto_rewrite=auto_rewrite,
        )
        turn_text = _trim_at_word_boundary(draft.body.strip(), LIVE_TURN_MAX_CHARS)
        effective_alias = sender_alias or effective_touch.sender_alias
        turn = ConversationTurnDraft(
            channel=channel,
            say=turn_text,
            attachments=list(draft.attachments[:MAX_TURN_ATTACHMENTS]),
            facts_used=draft.facts_used,
            from_sender=draft.from_sender
            or resolve_sender_for_channel(
                snapshot.identity, channel, sender_alias=effective_alias
            ),
            sent_at=draft.sent_at,
            hang_up=bool(draft.hang_up),
        )

        payload = dict(base_proposal.payload)
        payload["say"] = turn.say
        payload["hang_up"] = turn.hang_up
        payload["api_payload"] = turn.to_api_payload()
        proposal = self.make_proposal(
            snapshot,
            payload,
            kind="message",
            rationale=(
                f"Composed live {channel} turn {turn_index}/{MAX_CONVERSATION_TURNS} "
                f"({len(turn.say)} chars, hang_up={turn.hang_up})."
            ),
        )
        return turn, proposal

    async def analyze_signal(
        self,
        snapshot: Snapshot,
        *,
        target_event: dict[str, Any] | None,
    ) -> tuple[SignalAssessment, Proposal]:
        """Classify a target reply/event (or silence) into a structured SignalAssessment."""
        if not target_event:
            signal = SignalAssessment(
                engagement=0.0,
                suspicion=0.2,
                hook_hits=[],
                mood="silent",
                proposed_next="wait",
                reason="No target event received; awaiting pacing window or follow-up.",
            )
            proposal = self.make_proposal(
                snapshot,
                signal,
                kind="signals",
                rationale="Deterministic silence signal classification.",
            )
            return signal, proposal

        context: dict[str, Any] = {
            "mode": "signal",
            "target": snapshot.target.model_dump(mode="json"),
            "profile": snapshot.profile.model_dump(mode="json"),
            "hooks": [h.model_dump(mode="json") for h in snapshot.hooks],
            "conversation": _compact_conversation_for_llm(snapshot),
            "target_event": target_event,
        }
        signal = await self.run_json(context, SignalAssessment, temperature=0.2)

        known_hook_ids = {h.hook_id for h in snapshot.hooks}
        signal.hook_hits = [hid for hid in signal.hook_hits if hid in known_hook_ids]

        proposal = self.make_proposal(
            snapshot,
            signal,
            kind="signals",
            rationale=(
                f"Signal engagement={signal.engagement:.2f}, suspicion={signal.suspicion:.2f}, "
                f"next={signal.proposed_next}."
            ),
        )
        return signal, proposal

    async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
        """Shared BaseAgent entrypoint dispatching between compose, live_turn, and signal modes."""
        mode: MessagingMode = kwargs.get("mode", "compose")
        if mode == "signal":
            raw_event = kwargs.get("target_event")
            if raw_event is None:
                raw_event = kwargs.get("event")
            _, proposal = await self.analyze_signal(
                snapshot,
                target_event=raw_event,
            )
            return proposal

        raw_touch = kwargs.get("touch_spec")
        if raw_touch is None:
            raw_touch = kwargs.get("touch")
        touch_spec: TouchSpec | None = None
        if isinstance(raw_touch, TouchSpec):
            touch_spec = raw_touch
        elif isinstance(raw_touch, dict):
            touch_spec = TouchSpec.model_validate(raw_touch)

        raw_qa = kwargs.get("qa_feedback")
        qa_feedback: QAVerdict | None = None
        if isinstance(raw_qa, QAVerdict):
            qa_feedback = raw_qa
        elif isinstance(raw_qa, dict):
            qa_feedback = QAVerdict.model_validate(raw_qa)

        if mode == "live_turn":
            _, proposal = await self.compose_live_turn(
                snapshot,
                channel=kwargs.get("channel", "phone_call"),
                touch_spec=touch_spec,
                latest_signal=kwargs.get("latest_signal"),
                persona_says=kwargs.get("persona_says"),
                turn_index=int(kwargs.get("turn_index", 1)),
                current_story_time=kwargs.get("current_story_time"),
                registered_page_url=kwargs.get("registered_page_url"),
                channel_info=kwargs.get("channel_info"),
                sender_alias=kwargs.get("sender_alias"),
                sent_at=kwargs.get("sent_at"),
                auto_rewrite=bool(kwargs.get("auto_rewrite", True)),
            )
            return proposal

        _, proposal = await self.compose(
            snapshot,
            touch_spec=touch_spec,
            latest_signal=kwargs.get("latest_signal"),
            registered_page_url=kwargs.get("registered_page_url"),
            known_registered_urls=kwargs.get("known_registered_urls"),
            campaign_memory=kwargs.get("campaign_memory"),
            qa_feedback=qa_feedback,
            rejection_feedback=kwargs.get("rejection_feedback"),
            sent_at=kwargs.get("sent_at"),
            current_story_time=kwargs.get("current_story_time"),
            channel_info=kwargs.get("channel_info"),
            email_style=kwargs.get("email_style"),
            action_artifact=kwargs.get("action_artifact"),
            objective=kwargs.get("objective"),
            sender_alias=kwargs.get("sender_alias"),
            branded_html=kwargs.get("branded_html"),
            portrait_ref=kwargs.get("portrait_ref"),
            auto_rewrite=bool(kwargs.get("auto_rewrite", True)),
        )
        return proposal
