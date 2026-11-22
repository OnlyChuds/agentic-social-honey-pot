from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from src.models import CampaignPlan, Event, Objective
from src.store.db import RegisteredPage

DEFAULT_DISCLOSURE_THRESHOLD = 0.7
DEFAULT_ACTION_THRESHOLD = 0.75
_COMMITTED_ACTION_EVENT_TYPES: frozenset[str] = frozenset(
    {"action.taken", "approval.granted", "access.granted", "payment.changed"}
)

PageSubmissionClass = Literal["credentials", "disclosure_candidate", "step_up", "none"]

SECRET_FIELD_TOKENS: tuple[str, ...] = (
    "password",
    "passcode",
    "pin",
    "otp",
    "one_time_code",
    "auth_code",
    "verification_code",
    "mfa_code",
    "token",
    "api_key",
    "apikey",
    "secret",
    "card_number",
    "card_last4",
    "cvv",
    "iban",
    "ssn",
    "credential",
)

_BASIC_STEP_UP_TOKENS: tuple[str, ...] = (
    "email",
    "name",
    "first_name",
    "last_name",
    "full_name",
    "company",
    "org",
    "role",
    "title",
    "phone",
    "preferred_slot",
    "time_slot",
    "slot",
    "topic_interest",
    "dietary",
    "attendance",
)

_MASKED_SECRET_VALUE = "(entered, not shown)"


def _is_secret_field_name(name: str) -> bool:
    lower = name.strip().lower()
    if not lower:
        return False
    return any(tok in lower for tok in SECRET_FIELD_TOKENS)


def _is_basic_step_up_field(name: str) -> bool:
    lower = name.strip().lower()
    if not lower:
        return True
    return lower in _BASIC_STEP_UP_TOKENS or any(
        lower.endswith(f"_{tok}") or lower.startswith(f"{tok}_")
        for tok in ("email", "name", "slot", "phone", "company")
    )


def _extract_page_submission_fields_and_values(
    event: Event,
) -> tuple[set[str], dict[str, str], str, str]:
    data = event.payload.get("data")
    data_dict = data if isinstance(data, dict) else {}
    raw_fields = data_dict.get("fields") or event.payload.get("fields") or []
    raw_values = data_dict.get("values") or event.payload.get("values") or {}

    values: dict[str, str] = {}
    if isinstance(raw_values, dict):
        for k, v in raw_values.items():
            key = str(k).strip()
            if key:
                values[key] = "" if v is None else str(v).strip()

    submitted_fields = {str(f).strip().lower() for f in raw_fields if str(f).strip()}
    submitted_fields.update(k.lower() for k in values)
    page_url = str(data_dict.get("url") or event.payload.get("url") or "")
    page_id = str(data_dict.get("page_id") or event.payload.get("page_id") or "")
    return submitted_fields, values, page_url, page_id


def classify_page_submission(
    event: Event,
    registered_pages: list[RegisteredPage],
) -> PageSubmissionClass:
    """Classify a ``page.submitted`` event into credentials, disclosure_candidate, step_up, or none."""
    del registered_pages
    if event.type != "target_event" or event.payload.get("event") != "page.submitted":
        return "none"

    submitted_fields, values, _, _ = _extract_page_submission_fields_and_values(event)
    if not submitted_fields and not values:
        return "none"

    # 1) Credentials: ANY secret field submitted OR masked value "(entered, not shown)"
    has_masked_secret = any(v.lower() == _MASKED_SECRET_VALUE for v in values.values())
    has_secret_field = any(_is_secret_field_name(f) for f in submitted_fields)
    if has_masked_secret or has_secret_field:
        return "credentials"

    # 2) Disclosure candidate: non-basic internal-info fields with non-empty values
    substantive_non_basic = [
        (k, v)
        for k, v in values.items()
        if not _is_basic_step_up_field(k) and len(v.strip()) >= 4
    ]
    if substantive_non_basic:
        return "disclosure_candidate"

    # Non-basic fields submitted without values dict (legacy/minimal payload)
    if any(not _is_basic_step_up_field(f) for f in submitted_fields):
        return "disclosure_candidate"

    # 3) Basic contact/scheduling/overview details only -> foot-in-the-door trust step-up
    return "step_up"


def extract_page_submission_summary(event: Event) -> str | None:
    """Format non-secret ``page.submitted`` field values into a clean summary for ProfileAgent."""
    if event.type != "target_event" or event.payload.get("event") != "page.submitted":
        return None
    submitted_fields, values, page_url, _ = _extract_page_submission_fields_and_values(event)
    pairs: list[str] = []
    for k, v in values.items():
        if _is_secret_field_name(k) or v.lower() == _MASKED_SECRET_VALUE:
            continue
        if v:
            pairs.append(f"{k}={v}")
    if not pairs:
        non_secret = sorted(f for f in submitted_fields if not _is_secret_field_name(f))
        if not non_secret:
            return None
        pairs = non_secret
    url_part = f" on {page_url}" if page_url else ""
    return f"Target submitted form{url_part}: " + "; ".join(pairs)


@dataclass(slots=True)
class WinMatch:
    objective: Objective
    evidence_event_ids: list[str]
    confidence: float
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


def check_credentials_win(
    event: Event,
    registered_pages: list[RegisteredPage],
) -> WinMatch | None:
    """Detect a credentials objective match from a `page.submitted` target event.

    Matches exact API secret fields (password, passcode, pin, otp, token, api_key,
    card_number, iban, etc.) or any field masked as ``"(entered, not shown)"``.
    Never false-positives on basic-detail forms (email/name/scheduling).
    """
    if classify_page_submission(event, registered_pages) != "credentials":
        return None

    submitted_fields, values, page_url, page_id = _extract_page_submission_fields_and_values(event)
    matched_page: RegisteredPage | None = None
    for page in registered_pages:
        if (page_url and page.url == page_url) or (page_id and page.page_id == page_id):
            matched_page = page
            break

    secret_fields_submitted = sorted(
        {
            f
            for f in submitted_fields
            if _is_secret_field_name(f)
        }
        | {
            k.lower()
            for k, v in values.items()
            if v.lower() == _MASKED_SECRET_VALUE
        }
    )
    resolved_url = matched_page.url if matched_page is not None else page_url
    resolved_id = matched_page.page_id if matched_page is not None else page_id
    return WinMatch(
        objective="credentials",
        evidence_event_ids=[event.event_id],
        confidence=1.0,
        summary=(
            f"Target submitted credential fields {secret_fields_submitted}"
            + (f" on {resolved_url}" if resolved_url else "")
        ),
        details={
            "url": resolved_url,
            "page_id": resolved_id,
            "submitted_fields": sorted(submitted_fields),
            "secret_fields": secret_fields_submitted,
        },
    )


def check_action_win(
    event: Event,
    plan: CampaignPlan | None,
) -> WinMatch | None:
    """Detect an explicit action event win (never false-winning on ``attachment.opened`` steps).

    Per updated Target API rules, ``link.opened`` and ``attachment.opened`` are intermediate
    steps, not incidents. An ``action`` incident requires the target actually executing an
    action (approving, changing a payment detail, granting access), either via an explicit
    action event or via ``check_committed_action_win`` from reply/call classification.
    """
    del plan
    if event.type != "target_event":
        return None
    ev_name = str(event.payload.get("event") or "").strip().lower()
    if ev_name not in _COMMITTED_ACTION_EVENT_TYPES:
        return None

    data = event.payload.get("data")
    data_dict = data if isinstance(data, dict) else {}
    summary = str(data_dict.get("summary") or event.payload.get("body") or ev_name).strip()
    return WinMatch(
        objective="action",
        evidence_event_ids=[event.event_id],
        confidence=1.0,
        summary=f"Target performed requested action ({ev_name}): {summary}",
        details={"event": ev_name, **data_dict},
    )


def check_committed_action_win(
    action_candidates: list[dict[str, Any]] | None,
    *,
    threshold: float = DEFAULT_ACTION_THRESHOLD,
) -> WinMatch | None:
    """Confirm an action objective from profile-classified committed target actions."""
    if not action_candidates:
        return None

    best: dict[str, Any] | None = None
    best_conf = -1.0
    for cand in action_candidates:
        if not bool(cand.get("committed")):
            continue
        try:
            conf = float(cand.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if conf >= threshold and conf > best_conf:
            best = cand
            best_conf = conf

    if best is None:
        return None

    evidence_ref = str(best.get("evidence_event") or "").strip()
    evidence_ids = [evidence_ref] if evidence_ref else []
    action_desc = str(best.get("action") or "Target executed requested action").strip()
    return WinMatch(
        objective="action",
        evidence_event_ids=evidence_ids,
        confidence=best_conf,
        summary=f"Target committed requested action (confidence={best_conf:.2f}): {action_desc}",
        details=dict(best),
    )


def check_disclosure_win(
    disclosure_candidates: list[dict[str, Any]] | None,
    *,
    threshold: float = DEFAULT_DISCLOSURE_THRESHOLD,
) -> WinMatch | None:
    """Confirm a disclosure match from profile-classified non-public candidates."""
    if not disclosure_candidates:
        return None

    best: dict[str, Any] | None = None
    best_conf = -1.0
    for cand in disclosure_candidates:
        public_flag = cand.get("public_or_not")
        if isinstance(public_flag, str):
            is_public = public_flag.strip().lower() in {"public", "true", "yes", "uncertain"}
        else:
            is_public = bool(public_flag)
        if is_public:
            continue

        try:
            conf = float(cand.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if conf >= threshold and conf > best_conf:
            best = cand
            best_conf = conf

    if best is None:
        return None

    evidence_ref = str(best.get("evidence_event") or "").strip()
    evidence_ids = [evidence_ref] if evidence_ref else []
    claim = str(best.get("claim") or "Non-public information disclosed")
    return WinMatch(
        objective="disclosure",
        evidence_event_ids=evidence_ids,
        confidence=best_conf,
        summary=f"Target disclosed non-public detail (confidence={best_conf:.2f}): {claim}",
        details=dict(best),
    )


def evaluate_wins(
    events: list[Event],
    *,
    plan: CampaignPlan | None,
    registered_pages: list[RegisteredPage],
    disclosure_candidates: list[dict[str, Any]] | None = None,
    action_candidates: list[dict[str, Any]] | None = None,
    disclosure_threshold: float = DEFAULT_DISCLOSURE_THRESHOLD,
    action_threshold: float = DEFAULT_ACTION_THRESHOLD,
) -> WinMatch | None:
    """Evaluate target events, disclosures, and committed actions against all win conditions."""
    for event in events:
        cred = check_credentials_win(event, registered_pages)
        if cred is not None:
            return cred
        act = check_action_win(event, plan)
        if act is not None:
            return act
    if plan is not None and plan.objective == "credentials":
        return None
    committed_act = check_committed_action_win(action_candidates, threshold=action_threshold)
    if committed_act is not None:
        return committed_act
    return check_disclosure_win(disclosure_candidates, threshold=disclosure_threshold)
