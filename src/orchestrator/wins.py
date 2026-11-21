from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.models import CampaignPlan, Event, Objective
from src.store.db import RegisteredPage

DEFAULT_DISCLOSURE_THRESHOLD = 0.7


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
    """Detect a credentials objective match from a `page.submitted` target event."""
    if event.type != "target_event":
        return None
    if event.payload.get("event") != "page.submitted":
        return None

    data = event.payload.get("data")
    data_dict = data if isinstance(data, dict) else {}
    raw_fields = (
        data_dict.get("fields")
        or event.payload.get("fields")
        or []
    )
    submitted_fields = {str(f).strip().lower() for f in raw_fields if str(f).strip()}
    page_url = str(data_dict.get("url") or event.payload.get("url") or "")
    page_id = str(data_dict.get("page_id") or event.payload.get("page_id") or "")

    matched_page: RegisteredPage | None = None
    for page in registered_pages:
        if (page_url and page.url == page_url) or (page_id and page.page_id == page_id):
            matched_page = page
            break

    if matched_page is not None and matched_page.fields:
        required = {f.strip().lower() for f in matched_page.fields if f.strip()}
        if required.issubset(submitted_fields):
            return WinMatch(
                objective="credentials",
                evidence_event_ids=[event.event_id],
                confidence=1.0,
                summary=f"Target submitted required fields {sorted(required)} on {matched_page.url}",
                details={
                    "url": matched_page.url,
                    "page_id": matched_page.page_id,
                    "submitted_fields": sorted(submitted_fields),
                },
            )
        return None

    if "password" in submitted_fields or "passcode" in submitted_fields or "token" in submitted_fields:
        return WinMatch(
            objective="credentials",
            evidence_event_ids=[event.event_id],
            confidence=1.0,
            summary=f"Target submitted credential fields {sorted(submitted_fields)}",
            details={
                "url": page_url,
                "page_id": page_id,
                "submitted_fields": sorted(submitted_fields),
            },
        )
    return None


def check_action_win(
    event: Event,
    plan: CampaignPlan | None,
) -> WinMatch | None:
    """Detect an action objective match from an `attachment.opened` target event."""
    if event.type != "target_event":
        return None
    if event.payload.get("event") != "attachment.opened":
        return None
    if plan is None or plan.action_artifact is None:
        return None

    expected_filename = str(plan.action_artifact.get("filename") or "").strip().lower()
    expected_asset_id = str(plan.action_artifact.get("asset_id") or "").strip().lower()

    data = event.payload.get("data")
    data_dict = data if isinstance(data, dict) else {}
    opened_filename = str(
        data_dict.get("filename") or event.payload.get("filename") or ""
    ).strip().lower()
    opened_asset_id = str(
        data_dict.get("asset_id") or event.payload.get("asset_id") or ""
    ).strip().lower()

    matched = (
        bool(expected_filename and opened_filename == expected_filename)
        or bool(expected_asset_id and opened_asset_id == expected_asset_id)
        or (not expected_filename and not expected_asset_id)
    )

    if not matched:
        return None

    artifact_label = opened_filename or opened_asset_id or "action_artifact"
    return WinMatch(
        objective="action",
        evidence_event_ids=[event.event_id],
        confidence=1.0,
        summary=f"Target opened declared action artifact '{artifact_label}'",
        details={
            "filename": opened_filename,
            "asset_id": opened_asset_id,
        },
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
    disclosure_threshold: float = DEFAULT_DISCLOSURE_THRESHOLD,
) -> WinMatch | None:
    """Evaluate target events and disclosure candidates against all win conditions."""
    for event in events:
        cred = check_credentials_win(event, registered_pages)
        if cred is not None:
            return cred
        act = check_action_win(event, plan)
        if act is not None:
            return act
    return check_disclosure_win(disclosure_candidates, threshold=disclosure_threshold)
