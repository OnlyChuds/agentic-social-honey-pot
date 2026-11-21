from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from src.agents.base import BaseAgent
from src.agents.landing_pages import PageArtifactSpec
from src.agents.qa import check_domain_blocklist
from src.llm.image import IMAGE_TOOL_SCHEMAS, ImageClient, make_image_tool_handlers
from src.llm.text import TextLLMClient
from src.models import (
    BrandingManifest,
    LandingPageSpec,
    Proposal,
    QAVerdict,
    Snapshot,
)

logger = logging.getLogger(__name__)

IdentityMode = Literal["bootstrap", "update", "landing_page", "revise"]

_SAFE_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class ImagePromptSpec(BaseModel):
    asset: str = "master_portrait"
    prompt: str
    seed: int = 42
    size: int = 1024
    reference_asset: str | None = None


class IdentityOutput(BaseModel):
    manifest: BrandingManifest
    image_prompts: list[ImagePromptSpec] = Field(default_factory=list)
    page_html: PageArtifactSpec | None = None

    @model_validator(mode="after")
    def _require_core_sender_fields(self) -> IdentityOutput:
        missing = [
            field_name
            for field_name in ("sender_name", "domain", "email")
            if not getattr(self.manifest, field_name, "").strip()
        ]
        if missing:
            raise ValueError(
                "manifest is missing required non-empty fields: " + ", ".join(missing)
            )
        return self


def _slugify(text: str, fallback: str = "persona") -> str:
    slug = _SLUG_RE.sub("_", text.strip().lower()).strip("_")
    return slug or fallback


def _safe_hex_color(value: str, fallback: str) -> str:
    cleaned = value.strip()
    return cleaned if _SAFE_COLOR_RE.match(cleaned) else fallback


def _strip_duplicate_trailing_signature(body: str, manifest: BrandingManifest) -> str:
    """Remove a trailing plain-text copy of ``manifest.signature`` to avoid double signatures."""
    sig_lines = [line.strip() for line in manifest.signature.splitlines() if line.strip()]
    if not sig_lines:
        return body

    body_lines = body.rstrip().splitlines()
    # Strip trailing lines matching manifest signature lines (case-insensitive)
    idx = len(body_lines)
    sig_idx = len(sig_lines) - 1
    matched = 0
    while idx > 0 and sig_idx >= 0:
        cur = body_lines[idx - 1].strip()
        if not cur:
            idx -= 1
            continue
        if cur.lower() == sig_lines[sig_idx].lower():
            idx -= 1
            sig_idx -= 1
            matched += 1
        else:
            break

    if matched >= max(2, len(sig_lines) - 1) or (len(sig_lines) == 1 and matched == 1):
        return "\n".join(body_lines[:idx]).rstrip()
    return body


def wrap_branded_email_html(
    manifest: BrandingManifest,
    *,
    subject: str,
    body_html: str,
    portrait_ref: str | None = None,
) -> str:
    """Assemble a self-contained, offline-renderable HTML email using manifest branding."""
    primary = _safe_hex_color(manifest.palette[0], "#1f2937") if manifest.palette else "#1f2937"
    accent = (
        _safe_hex_color(manifest.palette[1], "#2563eb")
        if len(manifest.palette) > 1
        else "#2563eb"
    )
    font_stack = ", ".join(manifest.fonts) if manifest.fonts else "Inter, system-ui, sans-serif"

    safe_subject = html_lib.escape(subject)
    safe_company = html_lib.escape(manifest.company or manifest.sender_name or "Team")
    safe_sender = html_lib.escape(manifest.sender_name)
    safe_title = html_lib.escape(manifest.title)
    safe_email = html_lib.escape(manifest.email)
    safe_footer = html_lib.escape(manifest.footer_legal)
    safe_signature_html = "<br>".join(
        html_lib.escape(line) for line in manifest.signature.splitlines() if line.strip()
    )

    logo_block = manifest.logo_svg.strip() if manifest.logo_svg.strip().startswith("<svg") else ""
    portrait_html = ""
    if portrait_ref:
        safe_ref = html_lib.escape(portrait_ref, quote=True)
        portrait_html = (
            f'<td style="width:56px;vertical-align:top;padding-right:12px;">'
            f'<img src="{safe_ref}" alt="{safe_sender}" '
            f'style="width:48px;height:48px;border-radius:50%;object-fit:cover;'
            f'object-position:center 20%;display:block;">'
            f"</td>"
        )

    body_content = body_html.strip()
    if "<" not in body_content:
        body_content = _strip_duplicate_trailing_signature(body_content, manifest)
        paragraphs = [p.strip() for p in body_content.split("\n\n") if p.strip()]
        body_content = "".join(
            f'<p style="margin:0 0 14px 0;line-height:1.55;">'
            f"{html_lib.escape(p).replace(chr(10), '<br>')}</p>"
            for p in paragraphs
        )

    signature_section = ""
    if safe_signature_html or safe_sender:
        sig_inner = safe_signature_html or (
            f"<strong>{safe_sender}</strong>"
            + (f"<br>{safe_title}" if safe_title else "")
            + (f"<br>{safe_company}" if manifest.company else "")
            + (f"<br>{safe_email}" if safe_email else "")
        )
        signature_section = (
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            'style="margin-top:24px;border-top:1px solid #e5e7eb;padding-top:16px;width:100%;">'
            f"<tr>{portrait_html}"
            f'<td style="font-size:13px;line-height:1.5;color:#374151;">{sig_inner}</td>'
            "</tr></table>"
        )

    footer_section = ""
    if safe_footer:
        footer_section = (
            '<div style="padding:14px 28px;background:#f9fafb;border-top:1px solid #e5e7eb;'
            f'font-size:11px;line-height:1.45;color:#6b7280;">{safe_footer}</div>'
        )

    return (
        "<!doctype html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{safe_subject}</title>"
        "</head>"
        f'<body style="margin:0;padding:24px 12px;background:#f3f4f6;'
        f'font-family:{font_stack};color:#111827;">'
        '<div style="max-width:600px;margin:0 auto;background:#ffffff;'
        'border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">'
        f'<div style="padding:18px 28px;background:{primary};color:#ffffff;'
        'display:flex;align-items:center;gap:12px;">'
        f'<div style="display:inline-block;vertical-align:middle;">{logo_block}</div>'
        f'<div style="font-size:15px;font-weight:600;letter-spacing:0.01em;">{safe_company}</div>'
        "</div>"
        f'<div style="height:3px;background:{accent};"></div>'
        '<div style="padding:24px 28px;font-size:14px;color:#1f2937;">'
        f"{body_content}"
        f"{signature_section}"
        "</div>"
        f"{footer_section}"
        "</div>"
        "</body>"
        "</html>"
    )


def render_branded_landing_page_html(
    manifest: BrandingManifest,
    spec: LandingPageSpec,
) -> str:
    """Assemble a compact, self-contained HTML form page using manifest branding."""
    primary = _safe_hex_color(manifest.palette[0], "#1f2937") if manifest.palette else "#1f2937"
    accent = (
        _safe_hex_color(manifest.palette[1], "#2563eb")
        if len(manifest.palette) > 1
        else "#2563eb"
    )
    font_stack = ", ".join(manifest.fonts) if manifest.fonts else "Inter, system-ui, sans-serif"
    safe_company = html_lib.escape(manifest.company or manifest.sender_name or "Partner Portal")
    safe_title = html_lib.escape(spec.pretext or f"{safe_company} Sign In")
    safe_footer = html_lib.escape(
        manifest.footer_legal or f"{manifest.company or 'Portal'} · {manifest.email}"
    )
    logo_block = manifest.logo_svg.strip() if manifest.logo_svg.strip().startswith("<svg") else ""
    fields = list(spec.fields) if spec.fields else ["email", "password"]

    field_rows: list[str] = []
    for raw_field in fields:
        name = html_lib.escape(raw_field.strip() or "field", quote=True)
        label = html_lib.escape(raw_field.replace("_", " ").strip().title() or "Field")
        input_type = (
            "password"
            if "pass" in raw_field.lower() or "token" in raw_field.lower()
            else "email"
            if "email" in raw_field.lower()
            else "text"
        )
        field_rows.append(
            f'<div style="margin-bottom:14px;">'
            f'<label for="{name}" style="display:block;font-size:13px;font-weight:500;'
            f'margin-bottom:5px;color:#374151;">{label}</label>'
            f'<input id="{name}" name="{name}" type="{input_type}" required '
            f'style="width:100%;box-sizing:border-box;padding:9px 11px;border:1px solid #d1d5db;'
            f'border-radius:6px;font-size:14px;color:#111827;" />'
            f"</div>"
        )

    return (
        "<!doctype html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{safe_company} — {safe_title}</title>"
        "</head>"
        f'<body style="margin:0;padding:40px 16px;background:#f3f4f6;'
        f'font-family:{font_stack};color:#111827;">'
        '<div style="max-width:440px;margin:0 auto;background:#ffffff;'
        'border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;">'
        f'<div style="padding:18px 24px;background:{primary};color:#ffffff;'
        'display:flex;align-items:center;gap:12px;">'
        f"<div>{logo_block}</div>"
        f'<div style="font-size:15px;font-weight:600;">{safe_company}</div>'
        "</div>"
        f'<div style="height:3px;background:{accent};"></div>'
        '<div style="padding:24px;">'
        f'<h1 style="margin:0 0 8px 0;font-size:18px;font-weight:600;">{safe_title}</h1>'
        '<p style="margin:0 0 20px 0;font-size:13px;color:#4b5563;">'
        "Enter your details below to continue.</p>"
        '<form method="post">'
        f"{''.join(field_rows)}"
        f'<button type="submit" style="width:100%;padding:10px 14px;background:{accent};'
        'color:#ffffff;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;">'
        "Continue</button>"
        "</form>"
        "</div>"
        '<footer style="padding:12px 24px;background:#f9fafb;border-top:1px solid #e5e7eb;'
        f'font-size:11px;color:#6b7280;">{safe_footer}</footer>'
        "</div>"
        "</body>"
        "</html>"
    )


class IdentityAgent(BaseAgent):
    """Typed agent plumbing for BrandingManifest creation, validation, and portrait asset wiring."""

    agent_name = "identity"
    proposal_kind = "identity"

    def __init__(
        self,
        llm: TextLLMClient,
        *,
        image_client: ImageClient | None = None,
        assets_dir: Path = Path("data/assets"),
        prompts_dir: Path = Path("prompts"),
    ) -> None:
        super().__init__(llm, prompts_dir=prompts_dir)
        self.image_client = image_client
        self.assets_dir = assets_dir
        self.image_tool_schemas = IMAGE_TOOL_SCHEMAS

    def _build_context(
        self,
        snapshot: Snapshot,
        *,
        mode: IdentityMode = "bootstrap",
        sender_concept: str | None = None,
        org_branding: dict[str, Any] | None = None,
        landing_page_spec: LandingPageSpec | None = None,
        qa_feedback: QAVerdict | None = None,
        previous_html: str | None = None,
    ) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "mode": mode,
            "target": snapshot.target.model_dump(mode="json"),
            "profile": snapshot.profile.model_dump(mode="json"),
            "existing_manifest": (
                snapshot.identity.model_dump(mode="json")
                if snapshot.identity is not None
                else None
            ),
            "facts": [
                f.model_dump(mode="json")
                for f in snapshot.facts
                if f.status == "active"
            ],
        }
        if sender_concept is not None:
            ctx["sender_concept"] = sender_concept
        if org_branding is not None:
            ctx["org_branding"] = org_branding
        if landing_page_spec is not None:
            ctx["landing_page_spec"] = landing_page_spec.model_dump(mode="json")
        if qa_feedback is not None:
            ctx["qa_feedback"] = qa_feedback.model_dump(mode="json", by_alias=True)
        if previous_html is not None:
            ctx["previous_html"] = previous_html
        return ctx

    async def _materialize_images(
        self,
        output: IdentityOutput,
        *,
        lora_strength: float = 0.75,
    ) -> tuple[dict[str, str], list[str]]:
        """Execute image generation via ImageClient tool handlers with graceful degradation."""
        generated_paths: dict[str, str] = {}
        warnings: list[str] = []

        if self.image_client is None or not output.image_prompts:
            return generated_paths, warnings

        handlers = make_image_tool_handlers(self.image_client, output_dir=self.assets_dir)
        base_slug = _slugify(output.manifest.sender_name or output.manifest.company)

        for spec in output.image_prompts:
            filename = f"{base_slug}_{_slugify(spec.asset, 'portrait')}.png"
            try:
                if spec.reference_asset and spec.reference_asset in generated_paths:
                    res = await handlers["edit_image"](
                        {
                            "reference_path": generated_paths[spec.reference_asset],
                            "instruction": spec.prompt,
                            "filename": filename,
                            "seed": spec.seed,
                        }
                    )
                else:
                    res = await handlers["generate_portrait"](
                        {
                            "prompt": spec.prompt,
                            "filename": filename,
                            "seed": spec.seed,
                            "lora_strength": lora_strength,
                        }
                    )
                if isinstance(res, dict) and res.get("local_path"):
                    local_path = str(res["local_path"])
                    generated_paths[spec.asset] = local_path
                    if spec.asset == "master_portrait" or not output.manifest.portrait.local:
                        output.manifest.portrait.local = local_path
            except Exception as exc:  # noqa: BLE001
                msg = f"Image generation failed for '{spec.asset}': {type(exc).__name__}: {exc}"
                logger.warning(msg)
                warnings.append(msg)

        return generated_paths, warnings

    async def generate_portrait_variant(
        self,
        reference_path: Path,
        instruction: str,
        *,
        filename: str,
        seed: int = 42,
    ) -> dict[str, Any]:
        """Create a reference-guided avatar variant using ImageClient.edit_image."""
        if self.image_client is None:
            raise RuntimeError("ImageClient is not configured on IdentityAgent")
        handlers = make_image_tool_handlers(self.image_client, output_dir=self.assets_dir)
        result = await handlers["edit_image"](
            {
                "reference_path": str(reference_path),
                "instruction": instruction,
                "filename": filename,
                "seed": seed,
            }
        )
        return dict(result) if isinstance(result, dict) else {"result": result}

    async def build(
        self,
        snapshot: Snapshot,
        *,
        mode: IdentityMode = "bootstrap",
        sender_concept: str | None = None,
        org_branding: dict[str, Any] | None = None,
        landing_page_spec: LandingPageSpec | None = None,
        qa_feedback: QAVerdict | None = None,
        previous_html: str | None = None,
        extra_blocked_domains: Iterable[str] = (),
        generate_images: bool = True,
        strict_domain_check: bool = True,
    ) -> tuple[IdentityOutput, Proposal]:
        """Generate structured identity output, validate domain compliance, and build Proposal."""
        ctx = self._build_context(
            snapshot,
            mode=mode,
            sender_concept=sender_concept,
            org_branding=org_branding,
            landing_page_spec=landing_page_spec,
            qa_feedback=qa_feedback,
            previous_html=previous_html,
        )
        try:
            if (
                mode in {"landing_page", "revise"}
                and snapshot.identity is not None
                and landing_page_spec is not None
            ):
                output = await asyncio.wait_for(
                    self.run_json(ctx, IdentityOutput),
                    timeout=65.0,
                )
            else:
                output = await self.run_json(ctx, IdentityOutput)
        except Exception as exc:
            if (
                mode in {"landing_page", "revise"}
                and snapshot.identity is not None
                and landing_page_spec is not None
            ):
                logger.warning(
                    "Identity %s LLM call failed (%s: %s); falling back to branded template.",
                    mode,
                    type(exc).__name__,
                    exc,
                )
                fallback_html = render_branded_landing_page_html(
                    snapshot.identity,
                    landing_page_spec,
                )
                output = IdentityOutput(
                    manifest=snapshot.identity.model_copy(deep=True),
                    page_html=PageArtifactSpec(
                        pretext=landing_page_spec.pretext,
                        title=landing_page_spec.pretext or snapshot.identity.company or "Portal",
                        html=fallback_html,
                        fields=list(landing_page_spec.fields) or ["email", "password"],
                    ),
                )
            else:
                raise

        if (
            mode in {"landing_page", "revise"}
            and landing_page_spec is not None
            and (output.page_html is None or not output.page_html.html.strip())
        ):
            output.page_html = PageArtifactSpec(
                pretext=landing_page_spec.pretext,
                title=landing_page_spec.pretext or output.manifest.company or "Portal",
                html=render_branded_landing_page_html(output.manifest, landing_page_spec),
                fields=list(landing_page_spec.fields) or ["email", "password"],
            )

        # Preserve existing manifest branding/portrait fields on update/page/revise if omitted
        if snapshot.identity is not None:
            prev = snapshot.identity
            cur = output.manifest
            if not cur.portrait.local and prev.portrait.local:
                cur.portrait = prev.portrait.model_copy()
            if mode in {"landing_page", "revise"}:
                for attr in (
                    "concept",
                    "title",
                    "company",
                    "real_org",
                    "phone",
                    "bio",
                    "logo_svg",
                    "signature",
                    "footer_legal",
                ):
                    if not getattr(cur, attr) and getattr(prev, attr):
                        setattr(cur, attr, getattr(prev, attr))
                if not cur.palette and prev.palette:
                    cur.palette = list(prev.palette)
                if not cur.fonts and prev.fonts:
                    cur.fonts = list(prev.fonts)

        # Validate domain compliance against real-root-domain blocklist
        real_domains = list(extra_blocked_domains)
        if org_branding and isinstance(org_branding.get("real_domain"), str):
            real_domains.append(str(org_branding["real_domain"]))
        domains_to_check = [d for d in (output.manifest.domain, output.manifest.email) if d.strip()]
        domain_issues = check_domain_blocklist(domains_to_check, extra_blocked=real_domains)
        if domain_issues and strict_domain_check:
            details = "; ".join(issue.fix for issue in domain_issues)
            raise ValueError(f"Identity manifest failed domain validation: {details}")

        generated_assets: dict[str, str] = {}
        warnings: list[str] = []
        if generate_images and mode in {"bootstrap", "update"}:
            generated_assets, warnings = await self._materialize_images(output)

        payload = output.model_dump(mode="json")
        if generated_assets:
            payload["generated_assets"] = generated_assets
        if warnings:
            payload["warnings"] = warnings
        if domain_issues:
            payload["domain_issues"] = [i.model_dump(mode="json") for i in domain_issues]

        rationale = (
            f"Identity {mode} for '{output.manifest.sender_name or output.manifest.company}' "
            f"(domain={output.manifest.domain or 'unset'})."
        )
        proposal = self.make_proposal(snapshot, payload, rationale=rationale)
        return output, proposal

    async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
        """Shared BaseAgent entrypoint returning Proposal(kind='identity')."""
        raw_spec = kwargs.get("landing_page_spec")
        landing_page_spec: LandingPageSpec | None = None
        if isinstance(raw_spec, LandingPageSpec):
            landing_page_spec = raw_spec
        elif isinstance(raw_spec, dict):
            landing_page_spec = LandingPageSpec.model_validate(raw_spec)

        raw_qa = kwargs.get("qa_feedback")
        qa_feedback: QAVerdict | None = None
        if isinstance(raw_qa, QAVerdict):
            qa_feedback = raw_qa
        elif isinstance(raw_qa, dict):
            qa_feedback = QAVerdict.model_validate(raw_qa)

        _, proposal = await self.build(
            snapshot,
            mode=kwargs.get("mode", "bootstrap"),
            sender_concept=kwargs.get("sender_concept"),
            org_branding=kwargs.get("org_branding"),
            landing_page_spec=landing_page_spec,
            qa_feedback=qa_feedback,
            previous_html=kwargs.get("previous_html"),
            extra_blocked_domains=kwargs.get("extra_blocked_domains", ()),
            generate_images=bool(kwargs.get("generate_images", True)),
            strict_domain_check=bool(kwargs.get("strict_domain_check", True)),
        )
        return proposal
