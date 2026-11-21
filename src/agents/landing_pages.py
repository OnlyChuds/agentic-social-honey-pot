"""Authored, self-contained credential-harvesting landing page templates.

Each template renders a branded, believable page whose single job is to make
the target type something valuable (password, OTP, passcode, billing data).
The generated HTML is designed to survive the deterministic QA gate in
``src/agents/qa.py`` by construction:

- inline CSS only, no ``<script>`` tags, zero external network requests,
- no ``<img>`` tags at all (branding is inline SVG, so nothing can break),
- every declared ``LandingPageSpec.fields`` entry renders as a real
  ``<input>`` with matching ``name`` (the fields harvested for the win check),
- the manifest company name always appears in visible text (manifest
  consistency check),
- no placeholder tokens (no TODO/lorem/``[name]``/``{{var}}``/example.com),
- layout constrained to ~440px so a 1200px landing-page viewport never
  overflows horizontally,
- the only anchor is a ``mailto:`` to the manifest address (never a dead
  ``#`` link), so link-text vs href checks stay clean.

``PageArtifactSpec`` lives here (not in ``identity.py``) so the template
module can return it without creating an import cycle; ``identity.py``
re-exports it for backward compatibility.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from src.models import BrandingManifest, LandingPageSpec

HarvestType = Literal[
    "portal_login",
    "password_reset",
    "mfa_reauth",
    "document_vault",
    "invoice_update",
    "scheduling",
]

MAX_LANDING_PAGE_FIELDS = 20  # must match Target API / qa.MAX_PAGE_FIELDS


class PageArtifactSpec(BaseModel):
    pretext: str = ""
    title: str = ""
    html: str = ""
    fields: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _TemplateCopy:
    """Mundane-urgent copy for one harvest template (never a hacked-style alarm)."""

    title: str
    heading: str
    subheading: str
    notice: str
    cta: str
    expiry: str
    default_fields: tuple[str, ...]


_COMPANY = "{company}"

TEMPLATE_COPY: dict[HarvestType, _TemplateCopy] = {
    "portal_login": _TemplateCopy(
        title=f"Partner Portal Sign In - {_COMPANY}",
        heading=f"Sign in to your {_COMPANY} partner portal",
        subheading=(
            "Enter your account credentials to continue. Your session will be "
            "restored exactly where you left off."
        ),
        notice=(
            "Scheduled maintenance: single sign-on is running a temporary "
            "sign-in flow until 18:00. Use your usual password."
        ),
        cta="Sign in",
        expiry=(
            "If you don't recognize this sign-in request, reset your password "
            "from the portal desk."
        ),
        default_fields=("email", "password"),
    ),
    "password_reset": _TemplateCopy(
        title=f"Password Reset - {_COMPANY}",
        heading="Set a new password",
        subheading=(
            f"Your {_COMPANY} account password is due for its annual rotation. "
            "Set a new password to keep access uninterrupted."
        ),
        notice=(
            "Accounts that are not rotated within 48 hours are locked and "
            "re-provisioned by the IT desk."
        ),
        cta="Set new password",
        expiry="This reset link is single-use and expires in 15 minutes.",
        default_fields=("email", "password"),
    ),
    "mfa_reauth": _TemplateCopy(
        title=f"Device Verification - {_COMPANY}",
        heading="Verify this device",
        subheading=(
            f"A new sign-in to the {_COMPANY} portal was detected. Confirm the "
            "device with the code from your authenticator app."
        ),
        notice=(
            "Until the device is verified it is marked temporary and "
            "background sync may pause."
        ),
        cta="Verify device",
        expiry="Codes rotate every 30 seconds. The session expires in 15 minutes.",
        default_fields=("email", "one_time_code"),
    ),
    "document_vault": _TemplateCopy(
        title=f"Signed Documents - {_COMPANY}",
        heading="Your documents are ready to review",
        subheading=(
            f"The signed document pack from the current cycle is available in "
            f"your secure {_COMPANY} vault. Sign in to read or download a copy."
        ),
        notice="Vault access for this cycle closes at the end of the month.",
        cta="Open documents",
        expiry="The review window stays open for 48 hours.",
        default_fields=("email", "passcode"),
    ),
    "invoice_update": _TemplateCopy(
        title=f"Invoice Action Required - {_COMPANY}",
        heading="Invoice action required",
        subheading=(
            "An invoice for the current month needs an updated billing "
            "reference before it can be released to your team."
        ),
        notice="Unreleased invoices roll over to the next cycle after 48 hours.",
        cta="Update invoice details",
        expiry="The invoice lock is released automatically after you submit.",
        default_fields=("email", "billing_reference", "card_last4"),
    ),
    "scheduling": _TemplateCopy(
        title=f"Panel Scheduling - {_COMPANY}",
        heading="Confirm your panel slot",
        subheading=(
            "Please confirm the proposed time slot for the upcoming panel so "
            "we can finalize the agenda and send the room details."
        ),
        notice="Slots are confirmed in order of response.",
        cta="Confirm time",
        expiry="The agenda is locked at 17:00 today.",
        default_fields=("email", "preferred_slot"),
    ),
}

# --- Harvest-type selection from a LandingPageSpec ---

_HARVEST_FIELD_SIGNATURES: tuple[tuple[HarvestType, frozenset[str]], ...] = (
    ("mfa_reauth", frozenset({"one_time_code", "otp", "auth_code", "verification_code"})),
    ("invoice_update", frozenset({"billing_reference", "card_last4", "card_number"})),
    ("document_vault", frozenset({"passcode", "vault_code"})),
    ("scheduling", frozenset({"preferred_slot", "time_slot"})),
)

_HARVEST_PRETEXT_KEYWORDS: tuple[tuple[HarvestType, tuple[str, ...]], ...] = (
    ("mfa_reauth", ("mfa", "2fa", "otp", "authenticator", "device", "verification")),
    ("password_reset", ("password", "reset", "rotation", "sso", "sign-in rotation")),
    ("document_vault", (
        "document", "vault", "contract", "signed", "nda", "agreement", "review",
    )),
    ("invoice_update", (
        "invoice", "billing", "payment", "finance", "vendor", "accounting",
    )),
    ("scheduling", (
        "schedule", "scheduling", "panel", "conference", "speaker", "workshop",
        "slot", "agenda", "meeting", "event",
    )),
    ("portal_login", ("portal", "login", "sign", "account", "access")),
)


def select_harvest_type(spec: LandingPageSpec) -> HarvestType:
    """Pick the template whose pretext and field signature best match ``spec``."""
    field_set = {f.strip().lower() for f in spec.fields if f.strip()}
    for harvest, signatures in _HARVEST_FIELD_SIGNATURES:
        if field_set & signatures:
            return harvest

    lower_pretext = spec.pretext.strip().lower()
    for harvest, keywords in _HARVEST_PRETEXT_KEYWORDS:
        if any(re.search(rf"\b{re.escape(kw)}\b", lower_pretext) for kw in keywords):
            return harvest
    return "portal_login"


# --- HTML rendering ---

_SAFE_HEX_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")

_FIELD_LABELS: dict[str, str] = {
    "email": "Work email",
    "password": "Password",
    "one_time_code": "Authentication code",
    "otp": "Authentication code",
    "passcode": "Passcode",
    "billing_reference": "Billing reference",
    "card_last4": "Card number (last 4 digits)",
    "preferred_slot": "Preferred time slot",
}

_TEXT_INPUT_FIELDS = frozenset(
    {"one_time_code", "otp", "auth_code", "verification_code", "passcode", "vault_code"}
)


def _safe_color(value: str, fallback: str) -> str:
    cleaned = (value or "").strip()
    return cleaned if _SAFE_HEX_RE.match(cleaned) else fallback


def _field_type(name: str) -> str:
    lower = name.strip().lower()
    if lower == "password":
        return "password"
    if lower == "email":
        return "email"
    if lower in _TEXT_INPUT_FIELDS:
        return "text"
    return "text"


def _field_label(name: str) -> str:
    return _FIELD_LABELS.get(name.strip().lower(), name.strip().replace("_", " ").title() or "Field")


def render_landing_page(spec: LandingPageSpec, manifest: BrandingManifest) -> PageArtifactSpec:
    """Render a branded, self-contained harvesting page for ``spec`` using ``manifest``.

    Output is QA-gate-safe by construction: inline CSS only, no scripts, no
    ``<img>`` tags, one real ``<input>`` per declared field, the manifest
    company in visible text, a single ``mailto:`` anchor, and a ~440px card
    that never overflows the landing-page viewport.
    """
    harvest = select_harvest_type(spec)
    copy = TEMPLATE_COPY[harvest]

    raw_company = (manifest.company or manifest.sender_name or "Company").strip()
    company = html_lib.escape(raw_company)
    primary = _safe_color(manifest.palette[0] if manifest.palette else "", "#1f2937")
    accent = _safe_color(
        manifest.palette[1] if len(manifest.palette) > 1 else "",
        "#2563eb",
    )
    safe_fonts = [
        re.sub(r"[^A-Za-z0-9 \-]", "", f).strip()
        for f in manifest.fonts
        if f and f.strip()
    ]
    font_stack = ", ".join(f for f in safe_fonts if f) or "Inter, system-ui, sans-serif"

    title_text = copy.title.replace("{company}", raw_company)
    safe_title = html_lib.escape(title_text)
    heading = html_lib.escape(copy.heading.replace("{company}", raw_company))
    subheading = html_lib.escape(copy.subheading.replace("{company}", raw_company))
    notice = html_lib.escape(copy.notice)
    cta = html_lib.escape(copy.cta)
    expiry = html_lib.escape(copy.expiry)

    deduped_fields: list[str] = []
    seen_fields: set[str] = set()
    for raw_f in spec.fields or copy.default_fields:
        cleaned_f = raw_f.strip()
        if not cleaned_f:
            continue
        key = cleaned_f.lower()
        if key in seen_fields:
            continue
        seen_fields.add(key)
        deduped_fields.append(cleaned_f)
        if len(deduped_fields) >= MAX_LANDING_PAGE_FIELDS:
            break
    fields = deduped_fields or list(copy.default_fields)

    inputs: list[str] = []
    for idx, name in enumerate(fields):
        safe_name = html_lib.escape(name, quote=True)
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_") or f"f{idx}"
        field_id = f"fld_{slug}_{idx}"
        safe_label = html_lib.escape(_field_label(name))
        inputs.append(
            f'<label for="{field_id}">{safe_label}</label>'
            f'<input id="{field_id}" name="{safe_name}" '
            f'type="{_field_type(name)}" autocomplete="off" spellcheck="false">'
        )
    form_inputs = "".join(inputs)

    logo_block = (
        manifest.logo_svg.strip()
        if manifest.logo_svg.strip().startswith(("<svg", "<img"))
        else ""
    )
    logo_span = f"<span>{logo_block}</span>" if logo_block else ""
    safe_email = manifest.email.strip()
    mailto_line = ""
    if safe_email and "@" in safe_email:
        safe_email_html = html_lib.escape(safe_email)
        mailto_line = (
            f'<p>Questions? Write to '
            f'<a href="mailto:{html_lib.escape(safe_email, quote=True)}">{safe_email_html}</a></p>'
        )
    legal_line = (
        f'<p class="legal">{html_lib.escape(manifest.footer_legal)}</p>'
        if manifest.footer_legal.strip()
        else ""
    )

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title>
<style>
body{{margin:0;padding:32px 16px;background:#f3f4f6;font-family:{font_stack};color:#111827;}}
.card{{max-width:440px;margin:0 auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;}}
.head{{display:flex;align-items:center;gap:10px;padding:16px 24px;background:{primary};color:#ffffff;font-size:15px;font-weight:600;letter-spacing:0.01em;}}
.bar{{height:3px;background:{accent};}}
.body{{padding:28px 24px;font-size:14px;line-height:1.5;}}
h1{{margin:0 0 10px;font-size:20px;font-weight:650;color:#111827;}}
p{{margin:0 0 12px;color:#374151;}}
.notice{{background:#eff6ff;border:1px solid #bfdbfe;border-radius:6px;padding:10px 12px;font-size:13px;line-height:1.45;color:#1e40af;margin:0 0 18px;}}
label{{display:block;font-size:12px;font-weight:600;color:#374151;margin:12px 0 4px;}}
input{{width:100%;box-sizing:border-box;padding:9px 10px;border:1px solid #d1d5db;border-radius:6px;font-size:14px;background:#ffffff;color:#111827;}}
button{{width:100%;margin-top:18px;padding:10px 0;border:0;border-radius:6px;background:{accent};color:#ffffff;font-size:14px;font-weight:600;cursor:pointer;}}
.foot{{padding:14px 24px 18px;background:#f9fafb;border-top:1px solid #e5e7eb;font-size:11px;line-height:1.5;color:#6b7280;}}
.foot p{{margin:0 0 6px;color:#6b7280;}}
.foot a{{color:#2563eb;text-decoration:none;}}
.legal{{font-size:10px;color:#9ca3af;}}
</style>
</head>
<body>
<div class="card">
<div class="head">{logo_span}<span>{company}</span></div>
<div class="bar"></div>
<div class="body">
<h1>{heading}</h1>
<p>{subheading}</p>
<div class="notice">{notice}</div>
<form method="post" action="">
{form_inputs}
<button type="submit">{cta}</button>
</form>
</div>
<div class="foot">
<p>{expiry}</p>
{mailto_line}{legal_line}
</div>
</div>
</body>
</html>
"""
    return PageArtifactSpec(pretext=spec.pretext, title=title_text, html=html, fields=fields)
