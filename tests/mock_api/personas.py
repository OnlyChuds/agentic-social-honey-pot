from __future__ import annotations

from typing import Any, Literal

PersonaKind = Literal["eager", "cautious", "suspicious", "silent"]


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
            events.append(
                {
                    "type": "page.submitted",
                    "url": matched_page["url"],
                    "page_id": matched_page["page_id"],
                    "fields": list(matched_page.get("fields", [])),
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
            events.append(
                {
                    "type": "page.submitted",
                    "url": matched_page["url"],
                    "page_id": matched_page["page_id"],
                    "fields": list(matched_page.get("fields", [])),
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
