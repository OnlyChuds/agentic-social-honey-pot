from __future__ import annotations

import html as html_lib
import re
from collections.abc import Sequence
from pathlib import Path

from src.models import BrandingManifest
from src.render.screenshot import HtmlRenderer

_SAFE_HEX_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def _safe_color(value: str, fallback: str) -> str:
    cleaned = (value or "").strip()
    return cleaned if _SAFE_HEX_RE.match(cleaned) else fallback


def build_visual_artifact_html(
    *,
    title: str,
    subtitle: str,
    bullets: Sequence[str],
    manifest: BrandingManifest,
) -> str:
    """Build a crisp, self-contained HTML one-pager / agenda card for PNG rendering."""
    primary = _safe_color(manifest.palette[0] if manifest.palette else "", "#111827")
    accent = _safe_color(manifest.palette[1] if len(manifest.palette) > 1 else "", "#2563eb")
    company = html_lib.escape((manifest.company or manifest.sender_name or "Briefing").strip())
    safe_title = html_lib.escape(title.strip() or "Executive Brief")
    safe_subtitle = html_lib.escape(subtitle.strip())
    safe_sender = html_lib.escape(
        " · ".join(
            part
            for part in (manifest.sender_name.strip(), manifest.title.strip(), manifest.email.strip())
            if part
        )
    )
    safe_footer = html_lib.escape(manifest.footer_legal.strip() or company)
    logo_block = (
        manifest.logo_svg.strip()
        if manifest.logo_svg.strip().startswith("<svg")
        else ""
    )
    logo_html = (
        f'<div class="logo">{logo_block}</div>'
        if logo_block
        else ""
    )
    bullet_items = "".join(
        f"<li>{html_lib.escape(item.strip())}</li>"
        for item in bullets
        if item and item.strip()
    )
    subtitle_html = f'<p class="sub">{safe_subtitle}</p>' if safe_subtitle else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{safe_title}</title>
<style>
body{{margin:0;padding:32px;background:#f3f4f6;font-family:Inter,system-ui,-apple-system,sans-serif;color:#111827;}}
.sheet{{max-width:680px;margin:0 auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.06);}}
.top{{display:flex;align-items:center;justify-content:space-between;padding:20px 28px;background:{primary};color:#ffffff;}}
.brand{{display:flex;align-items:center;gap:12px;font-size:15px;font-weight:600;letter-spacing:0.01em;}}
.logo{{display:inline-flex;align-items:center;justify-content:center;background:#ffffff;border-radius:6px;padding:4px;line-height:0;}}
.tag{{font-size:11px;text-transform:uppercase;letter-spacing:0.08em;opacity:0.85;}}
.bar{{height:4px;background:{accent};}}
.content{{padding:28px 32px;}}
h1{{margin:0 0 8px;font-size:24px;line-height:1.25;color:#111827;}}
.sub{{margin:0 0 20px;font-size:14px;line-height:1.5;color:#4b5563;}}
ul{{margin:0;padding:0;list-style:none;display:grid;gap:12px;}}
li{{padding:12px 14px;background:#f9fafb;border-left:3px solid {accent};border-radius:6px;font-size:14px;line-height:1.45;color:#1f2937;}}
.foot{{padding:16px 32px;background:#f9fafb;border-top:1px solid #e5e7eb;display:flex;justify-content:space-between;gap:12px;font-size:11px;color:#6b7280;}}
</style>
</head>
<body>
<div class="sheet">
  <div class="top">
    <div class="brand">{logo_html}<span>{company}</span></div>
    <span class="tag">Briefing Sheet</span>
  </div>
  <div class="bar"></div>
  <div class="content">
    <h1>{safe_title}</h1>
    {subtitle_html}
    <ul>{bullet_items}</ul>
  </div>
  <div class="foot">
    <span>{safe_sender}</span>
    <span>{safe_footer}</span>
  </div>
</div>
</body>
</html>
"""


async def render_visual_artifact_png(
    renderer: HtmlRenderer,
    *,
    title: str,
    subtitle: str,
    bullets: Sequence[str],
    manifest: BrandingManifest,
    out_path: Path,
) -> Path:
    """Render a branded PNG one-pager/agenda via Playwright for ``POST /v1/assets``."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = build_visual_artifact_html(
        title=title,
        subtitle=subtitle,
        bullets=bullets,
        manifest=manifest,
    )
    await renderer.render(html, mode="email", save_path=out_path)
    return out_path
