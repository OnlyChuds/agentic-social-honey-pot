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
from src.agents.domains import derive_sender_email, ensure_lookalike_domain
from src.agents.landing_pages import (
    PageArtifactSpec as PageArtifactSpec,  # noqa: PLC0414
)
from src.agents.landing_pages import render_landing_page
from src.agents.qa import (
    DEFAULT_BLOCKED_ROOT_DOMAINS,
    check_domain_blocklist,
    extract_domain,
    is_domain_blocked,
)
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


_TRAILING_HTML_P_RE = re.compile(r"<p\b[^>]*>((?:(?!</p>)[\s\S])*)</p>\s*$", re.IGNORECASE)
_INLINE_TAG_RE = re.compile(r"<[^>]+>")


def _strip_duplicate_trailing_signature(body: str, manifest: BrandingManifest) -> str:
    """Remove trailing duplicate signature/name/footer lines (plain text or trailing HTML <p>)."""
    sig_lines = [line.strip() for line in manifest.signature.splitlines() if line.strip()]
    known_tail_lines = {line.lower() for line in sig_lines}
    for extra in (manifest.sender_name, manifest.title, manifest.company, manifest.footer_legal):
        if extra and extra.strip():
            known_tail_lines.add(extra.strip().lower())
    if not known_tail_lines:
        return body

    signoffs = {"best", "regards", "best regards", "warm regards", "thanks", "cheers"}

    # Handle HTML paragraph bodies ending with a redundant signature <p>...</p>
    if "<" in body:
        cleaned = body.rstrip()
        while True:
            m = _TRAILING_HTML_P_RE.search(cleaned)
            if not m:
                break
            inner_lines = [
                html_lib.unescape(_INLINE_TAG_RE.sub("", part)).strip()
                for part in re.split(r"<br\s*/?>|\n", m.group(1), flags=re.IGNORECASE)
            ]
            non_empty = [ln for ln in inner_lines if ln]
            if (
                non_empty
                and all(
                    ln.lower().rstrip(",.!") in known_tail_lines
                    or ln.lower().rstrip(",.!") in signoffs
                    for ln in non_empty
                )
                and any(ln.lower().rstrip(",.!") in known_tail_lines for ln in non_empty)
            ):
                cleaned = cleaned[: m.start()].rstrip()
            else:
                break
        return cleaned

    body_lines = body.rstrip().splitlines()
    idx = len(body_lines)
    matched = 0
    while idx > 0:
        cur = body_lines[idx - 1].strip()
        if not cur:
            idx -= 1
            continue
        cur_norm = cur.lower().rstrip(",.!")
        if cur_norm in known_tail_lines:
            idx -= 1
            matched += 1
        elif matched > 0 and cur_norm in signoffs:
            idx -= 1
        else:
            break

    if matched >= 1:
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

    logo_block = (
        manifest.logo_svg.strip()
        if manifest.logo_svg.strip().startswith(("<svg", "<img"))
        else ""
    )
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

    body_content = _strip_duplicate_trailing_signature(body_html.strip(), manifest)
    if "<" not in body_content:
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
        + (
            f'<div style="display:inline-flex;align-items:center;justify-content:center;'
            f'background:#ffffff;border-radius:6px;padding:4px;line-height:0;">{logo_block}</div>'
            if logo_block
            else ""
        )
        + f'<div style="font-size:15px;font-weight:600;letter-spacing:0.01em;">{safe_company}</div>'
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
            asset_l = spec.asset.strip().lower()
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
                elif any(tok in asset_l for tok in ("logo", "badge", "icon", "emblem")):
                    dim = max(256, min(1024, int(spec.size or 512)))
                    res = await handlers["generate_fast_image"](
                        {
                            "prompt": spec.prompt,
                            "filename": filename,
                            "width": dim,
                            "height": dim,
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
                    if spec.asset == "master_portrait" or (
                        not output.manifest.portrait.local
                        and not any(tok in asset_l for tok in ("logo", "badge", "icon", "emblem"))
                    ):
                        output.manifest.portrait.local = local_path
            except Exception as exc:  # noqa: BLE001
                msg = f"Image generation failed for '{spec.asset}': {type(exc).__name__}: {exc}"
                logger.warning(msg)
                warnings.append(msg)

        return generated_paths, warnings

    @staticmethod
    def _fallback_bootstrap_identity(
        snapshot: Snapshot,
        sender_concept: str | None = None,
    ) -> IdentityOutput:
        """Deterministic fallback persona so bootstrap never leaves snapshot.identity=None."""
        target_org = snapshot.target.org or "TargetOrg"
        concept = sender_concept or "DACH enterprise engineering & GTM partner"
        company = "NordPeak Advisory"
        domain = "nordpeak-advisory.de"
        sender_name = "Lukas Weber"
        title = "Partner, Enterprise Architecture"
        email = f"lukas.weber@{domain}"
        manifest = BrandingManifest(
            concept=concept,
            sender_name=sender_name,
            title=title,
            company=company,
            real_org=target_org,
            domain=domain,
            email=email,
            phone="+49 89 5550 2418",
            bio=(
                "I advise DACH B2B software founders on enterprise architecture and technical "
                "partnerships. Previously worked across Munich scale-ups on high-throughput data systems."
            ),
            logo_svg="",
            palette=["#111827", "#2563eb", "#f3f4f6"],
            fonts=["Inter", "system-ui", "sans-serif"],
            signature=f"{sender_name}\n{title}\n{company} GmbH",
            footer_legal=f"{company} GmbH · Munich",
        )
        return IdentityOutput(
            manifest=manifest,
            image_prompts=[
                ImagePromptSpec(
                    asset="master_portrait",
                    prompt=(
                        "casual smartphone headshot of Lukas Weber, a 36-year-old German male "
                        "technology advisor in a dark navy overshirt over a plain t-shirt, "
                        "natural daylight in a modern Munich office, relaxed expression, unretouched"
                    ),
                    seed=42,
                    size=1024,
                ),
                ImagePromptSpec(
                    asset="logo",
                    prompt=(
                        "minimalist flat geometric mountain-peak tech company logo mark for "
                        "NordPeak Advisory, navy and blue shapes on pure white background, centered, no text"
                    ),
                    seed=42,
                    size=512,
                ),
            ],
        )

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

    @staticmethod
    def _repair_blocked_domain(
        manifest: BrandingManifest,
        real_domains: list[str],
        *,
        hint: str | None = None,
    ) -> bool:
        """Replace a blocked manifest domain/email with a generated lookalike + derived address.

        Returns True when the manifest was changed (or the email was re-derived onto
        an already-valid domain). The lookalike comes from the deterministic generator
        in ``src.agents.domains`` so the result passes ``check_domain_blocklist`` by
        construction when a candidate exists.
        """
        roots = [d for d in real_domains if d.strip()]
        if not roots:
            return False
        current = extract_domain(manifest.domain)
        real_root = next((d for d in roots if extract_domain(d) != current), roots[0])
        others = [d for d in roots if d != real_root]
        safe_domain = ensure_lookalike_domain(
            manifest.domain,
            real_root,
            extra_blocked=others,
            hint=hint,
        )
        if not safe_domain:
            return False
        if safe_domain == current:
            # Domain itself was fine; only the address sat on a blocked root.
            manifest.email = derive_sender_email(manifest.sender_name, safe_domain)
            return True
        manifest.domain = safe_domain
        manifest.email = derive_sender_email(manifest.sender_name, safe_domain)
        return True

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
        if (
            mode == "landing_page"
            and snapshot.identity is not None
            and landing_page_spec is not None
        ):
            manifest_copy = snapshot.identity.model_copy(deep=True)
            output = IdentityOutput(
                manifest=manifest_copy,
                page_html=render_landing_page(landing_page_spec, manifest_copy),
            )
        else:
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
                    mode == "revise"
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
                    output = IdentityOutput(
                        manifest=snapshot.identity.model_copy(deep=True),
                        page_html=render_landing_page(landing_page_spec, snapshot.identity),
                    )
                elif mode == "bootstrap":
                    logger.warning(
                        "Identity bootstrap LLM call failed (%s: %s); using deterministic fallback persona.",
                        type(exc).__name__,
                        exc,
                    )
                    output = self._fallback_bootstrap_identity(snapshot, sender_concept=sender_concept)
                else:
                    raise

        if (
            mode == "bootstrap"
            and self.image_client is not None
            and not any(
                any(tok in spec.asset.lower() for tok in ("logo", "badge", "icon", "emblem"))
                for spec in output.image_prompts
            )
        ):
            company_label = output.manifest.company or output.manifest.sender_name or "Company"
            output.image_prompts.append(
                ImagePromptSpec(
                    asset="logo",
                    prompt=(
                        f"minimalist flat geometric tech company logo icon for {company_label}, "
                        "crisp vector emblem on pure white background, centered, no text"
                    ),
                    seed=42,
                    size=512,
                )
            )

        if (
            mode in {"landing_page", "revise"}
            and landing_page_spec is not None
            and (output.page_html is None or not output.page_html.html.strip())
        ):
            output.page_html = render_landing_page(landing_page_spec, output.manifest)

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

        # Validate domain compliance against real-root-domain blocklist; when the
        # manifest lands on a blocked root, repair it with the lookalike generator
        # (offensive half of qa.check_domain_blocklist) before failing.
        real_domains = list(extra_blocked_domains)
        if org_branding and isinstance(org_branding.get("real_domain"), str):
            real_domains.append(str(org_branding["real_domain"]))
        domains_to_check = [d for d in (output.manifest.domain, output.manifest.email) if d.strip()]
        domain_issues = check_domain_blocklist(domains_to_check, extra_blocked=real_domains)
        if domain_issues:
            # When the block comes from the default blocklist (not caller-supplied
            # extras), derive the matched real root so the repairer has a base.
            blocked_pool = {
                extract_domain(d) for d in (*DEFAULT_BLOCKED_ROOT_DOMAINS, *real_domains) if d.strip()
            }
            blocked_pool.discard("")
            matched_root = is_domain_blocked(output.manifest.domain, blocked_pool)
            if matched_root is None:
                matched_root = is_domain_blocked(output.manifest.email, blocked_pool)
            repair_roots = [matched_root] if matched_root else list(real_domains)
            repaired = self._repair_blocked_domain(
                output.manifest,
                repair_roots,
                hint=landing_page_spec.pretext if landing_page_spec is not None else None,
            )
            if repaired:
                logger.info(
                    "Repaired blocked identity domain to %s (email %s).",
                    output.manifest.domain,
                    output.manifest.email,
                )
                if mode == "landing_page" and landing_page_spec is not None:
                    output.page_html = render_landing_page(landing_page_spec, output.manifest)
                domains_to_check = [
                    d for d in (output.manifest.domain, output.manifest.email) if d.strip()
                ]
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
