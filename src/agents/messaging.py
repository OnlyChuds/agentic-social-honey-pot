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
from src.agents.identity import wrap_branded_email_html
from src.agents.qa import check_disallowed_meta_text
from src.llm.text import TextLLMClient
from src.models import (
    BrandingManifest,
    Channel,
    Proposal,
    QAVerdict,
    Snapshot,
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


def resolve_sender_for_channel(
    manifest: BrandingManifest | None,
    channel: Channel,
) -> SenderAddress | None:
    """Resolve the Target API ``from`` object from ``BrandingManifest`` for a given channel."""
    if manifest is None:
        return None
    address = manifest.phone if channel in PHONE_CHANNELS and manifest.phone else manifest.email
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

    for meta_issue in check_disallowed_meta_text(combined):
        issues.append(meta_issue.fix)

    return issues


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
        qa_feedback: QAVerdict | None = None,
        rejection_feedback: str | None = None,
        sent_at: str | None = None,
        branded_html: bool | None = None,
        portrait_ref: str | None = None,
        auto_rewrite: bool = True,
    ) -> tuple[MessageDraft, Proposal]:
        """Compose a schema-validated outbound message proposal for the next planned touch."""
        active_touch = touch_spec or self._next_planned_touch(snapshot)
        is_first_contact = snapshot.campaign.budget.used == 0 and not snapshot.conversation.turns

        ranked_hooks = sorted(
            [h for h in snapshot.hooks if h.status != "exhausted"],
            key=lambda h: (h.engagement.score, h.confidence),
            reverse=True,
        )

        context: dict[str, Any] = {
            "mode": "compose",
            "target": snapshot.target.model_dump(mode="json"),
            "manifest": _compact_manifest_for_llm(snapshot.identity),
            "profile": snapshot.profile.model_dump(mode="json"),
            "hooks": [h.model_dump(mode="json") for h in ranked_hooks],
            "facts": [f.model_dump(mode="json") for f in snapshot.facts if f.status == "active"],
            "touch": active_touch.model_dump(mode="json") if active_touch is not None else None,
            "conversation": _compact_conversation_for_llm(snapshot),
            "registered_page_url": registered_page_url,
            "is_first_contact": is_first_contact,
        }
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

        draft = await self.run_json(context, MessageDraft)
        lint_issues = lint_message_style(draft)
        if lint_issues and auto_rewrite:
            retry_context = dict(context)
            retry_context["previous_draft"] = draft.model_dump(mode="json")
            retry_context["style_lint_issues"] = lint_issues
            draft = await self.run_json(retry_context, MessageDraft)
            lint_issues = lint_message_style(draft)

        draft, limit_warnings = enforce_channel_limits(draft)
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
            draft.from_sender = resolve_sender_for_channel(snapshot.identity, draft.channel)
        if sent_at is not None:
            draft.sent_at = sent_at

        should_wrap_html = (
            branded_html
            if branded_html is not None
            else (
                draft.channel == "email"
                and snapshot.identity is not None
                and bool(snapshot.identity.logo_svg or snapshot.identity.palette)
            )
        )
        if (
            should_wrap_html
            and draft.channel == "email"
            and snapshot.identity is not None
            and "<!doctype html>" not in draft.body.lower()
        ):
            effective_portrait = portrait_ref
            if effective_portrait is None and snapshot.identity.portrait.api_asset_id:
                effective_portrait = f"asset://{snapshot.identity.portrait.api_asset_id}"
            draft.body = wrap_branded_email_html(
                snapshot.identity,
                subject=draft.subject or "",
                body_html=draft.body,
                portrait_ref=effective_portrait,
            )

        warnings = [*limit_warnings, *fact_warnings, *lint_issues]
        payload = draft.model_dump(mode="json")
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

        draft, base_proposal = await self.compose(
            snapshot,
            touch_spec=effective_touch,
            latest_signal=latest_signal,
            sent_at=sent_at,
            branded_html=False,
            auto_rewrite=auto_rewrite,
        )
        turn_text = _trim_at_word_boundary(draft.body.strip(), LIVE_TURN_MAX_CHARS)
        turn = ConversationTurnDraft(
            channel=channel,
            say=turn_text,
            attachments=list(draft.attachments[:MAX_TURN_ATTACHMENTS]),
            facts_used=draft.facts_used,
            from_sender=draft.from_sender or resolve_sender_for_channel(snapshot.identity, channel),
            sent_at=draft.sent_at,
        )

        payload = dict(base_proposal.payload)
        payload["say"] = turn.say
        payload["api_payload"] = turn.to_api_payload()
        proposal = self.make_proposal(
            snapshot,
            payload,
            kind="message",
            rationale=f"Composed live {channel} turn ({len(turn.say)} chars).",
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
        if known_hook_ids:
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
                sent_at=kwargs.get("sent_at"),
                auto_rewrite=bool(kwargs.get("auto_rewrite", True)),
            )
            return proposal

        _, proposal = await self.compose(
            snapshot,
            touch_spec=touch_spec,
            latest_signal=kwargs.get("latest_signal"),
            registered_page_url=kwargs.get("registered_page_url"),
            qa_feedback=qa_feedback,
            rejection_feedback=kwargs.get("rejection_feedback"),
            sent_at=kwargs.get("sent_at"),
            branded_html=kwargs.get("branded_html"),
            portrait_ref=kwargs.get("portrait_ref"),
            auto_rewrite=bool(kwargs.get("auto_rewrite", True)),
        )
        return proposal
