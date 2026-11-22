from __future__ import annotations

from typing import Any, Literal

PersonaKind = Literal["eager", "cautious", "suspicious", "silent"]

_SECRET_FIELD_TOKENS: tuple[str, ...] = (
    "password",
    "passcode",
    "pin",
    "otp",
    "one_time_code",
    "auth_code",
    "verification_code",
    "token",
    "api_key",
    "secret",
    "card_number",
    "card_last4",
    "iban",
    "cvv",
)


def _mock_submitted_values(fields: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_f in fields:
        f = raw_f.strip()
        if not f:
            continue
        fl = f.lower()
        if any(tok in fl for tok in _SECRET_FIELD_TOKENS):
            values[f] = "(entered, not shown)"
        elif "email" in fl:
            values[f] = "julius@revel8.ai"
        elif "stack" in fl or "tool" in fl or "system" in fl or "infra" in fl:
            values[f] = "internal-k8s-eu02 with Okta SSO and Snowflake prod warehouse"
        elif "note" in fl or "detail" in fl or "project" in fl or "priority" in fl:
            values[f] = "Rolling out internal Q4 SOC2 automation pipeline"
        elif "slot" in fl or "time" in fl:
            values[f] = "Thursday 14:00 CET"
        elif "name" in fl:
            values[f] = "Julius Muth"
        else:
            values[f] = f"confirmed_{fl}"
    return values


def mock_live_greeting(persona: PersonaKind, channel: str) -> str:
    if persona == "suspicious":
        return f"Hello? Who is calling on {channel}?"
    if persona == "cautious":
        return "Hi, this is Julius — who am I speaking with?"
    return "Hi, Julius speaking — how can I help?"


def evaluate_live_turn(
    *,
    persona: PersonaKind,
    channel: str,
    say: str,
    turn_number: int,
    registered_pages: list[dict[str, Any]],
    attachments: list[Any],
) -> tuple[str, bool, list[dict[str, Any]]]:
    """Return ``(persona_says, ended, events)`` for one live conversation turn."""
    events = evaluate_persona_reaction(
        persona=persona,
        channel=channel,
        body=say,
        touch_number=turn_number,
        registered_pages=registered_pages,
        attachments=attachments,
    )
    reply_text = ""
    for ev in events:
        if ev.get("type") == "reply" and not reply_text:
            reply_text = str(ev.get("body") or "")

    if persona == "silent":
        return "Sorry, I have to jump into another meeting.", True, []
    if persona == "suspicious":
        ended = turn_number >= 2
        return (
            reply_text or "I can't verify this over the phone — send an internal ticket instead.",
            ended,
            events,
        )
    ended = any(ev.get("type") in {"page.submitted", "attachment.opened"} for ev in events)
    if not reply_text:
        reply_text = "Got it, thanks for walking me through that."
    return reply_text, ended, events


def evaluate_persona_reaction(
    *,
    persona: PersonaKind,
    channel: str,
    body: str,
    touch_number: int,
    registered_pages: list[dict[str, Any]],
    attachments: list[Any],
) -> list[dict[str, Any]]:
    """Deterministic scripted target reactions for offline dry runs."""
    if persona == "silent":
        return []

    matched_page: dict[str, Any] | None = None
    for page in registered_pages:
        url = str(page.get("url", ""))
        if url and url in body:
            matched_page = page
            break

    opened_attachment: dict[str, Any] | None = None
    if attachments:
        first = attachments[0]
        if isinstance(first, dict):
            opened_attachment = {"filename": str(first.get("filename", ""))}
        elif isinstance(first, str):
            opened_attachment = {"asset_id": first}

    if persona == "eager":
        events: list[dict[str, Any]] = [
            {
                "type": "reply",
                "channel": channel,
                "body": "Thanks for reaching out! Happy to look into this right away.",
            }
        ]
        if matched_page is not None:
            events.append({"type": "link.opened", "url": matched_page["url"], "page_id": matched_page["page_id"]})
            fields = list(matched_page.get("fields", []))
            events.append(
                {
                    "type": "page.submitted",
                    "url": matched_page["url"],
                    "page_id": matched_page["page_id"],
                    "fields": fields,
                    "values": _mock_submitted_values(fields),
                }
            )
        if opened_attachment is not None:
            events.append({"type": "attachment.opened", **opened_attachment})
        return events

    if persona == "cautious":
        if touch_number == 1:
            return [
                {
                    "type": "reply",
                    "channel": channel,
                    "body": "Interesting — could you share a bit more context on which team this is for?",
                }
            ]
        if touch_number == 2:
            return [
                {
                    "type": "reply",
                    "channel": channel,
                    "body": (
                        "Got it, that makes sense. Internally we run our staging cluster on "
                        "internal-k8s-eu02 and rotate deployments every Thursday."
                    ),
                }
            ]
        events = [
            {
                "type": "reply",
                "channel": channel,
                "body": "Looks reasonable, taking care of it now.",
            }
        ]
        if matched_page is not None:
            events.append({"type": "link.opened", "url": matched_page["url"], "page_id": matched_page["page_id"]})
            fields = list(matched_page.get("fields", []))
            events.append(
                {
                    "type": "page.submitted",
                    "url": matched_page["url"],
                    "page_id": matched_page["page_id"],
                    "fields": fields,
                    "values": _mock_submitted_values(fields),
                }
            )
        if opened_attachment is not None:
            events.append({"type": "attachment.opened", **opened_attachment})
        return events

    # persona == "suspicious"
    return [
        {
            "type": "reply",
            "channel": channel,
            "body": (
                "Who are you and why are you contacting me from an unfamiliar address? "
                "Please verify through our official channel."
            ),
        }
    ]
