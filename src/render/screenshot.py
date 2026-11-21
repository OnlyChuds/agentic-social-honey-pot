from __future__ import annotations

import base64
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Self, cast

from playwright.async_api import Browser, ConsoleMessage, Playwright, Route, async_playwright

RenderMode = Literal["email", "landing_page"]

_VIEWPORTS: dict[RenderMode, tuple[int, int]] = {
    "email": (700, 900),
    "landing_page": (1200, 800),
}

ASSET_URI_RE = re.compile(r"asset://([A-Za-z0-9_.\-/]+)")

_DOM_INSPECT_SCRIPT = """
() => {
  const imgs = Array.from(document.querySelectorAll('img'));
  const brokenImages = imgs
    .filter((img) => !img.complete || img.naturalWidth === 0)
    .map((img) => img.getAttribute('data-original-src') || img.getAttribute('src') || img.alt || '<img without src>');

  const links = Array.from(document.querySelectorAll('a')).map((a) => ({
    text: (a.innerText || a.textContent || '').trim(),
    href: (a.getAttribute('href') || '').trim(),
  }));

  const formFields = Array.from(document.querySelectorAll('input, textarea, select')).map((el) => ({
    name: (el.getAttribute('name') || '').trim(),
    field_type: (el.getAttribute('type') || el.tagName.toLowerCase()).trim().toLowerCase(),
    element_id: (el.getAttribute('id') || '').trim(),
    placeholder: (el.getAttribute('placeholder') || '').trim(),
  }));

  const docEl = document.documentElement;
  const body = document.body;
  const scrollWidth = Math.max(docEl ? docEl.scrollWidth : 0, body ? body.scrollWidth : 0);
  const scrollHeight = Math.max(docEl ? docEl.scrollHeight : 0, body ? body.scrollHeight : 0);
  const visibleText = body && body.innerText ? body.innerText.trim() : '';

  return {
    broken_images: brokenImages,
    links,
    form_fields: formFields,
    has_horizontal_overflow: scrollWidth > window.innerWidth + 1,
    width: Math.max(window.innerWidth, scrollWidth),
    height: Math.max(window.innerHeight, scrollHeight),
    visible_text: visibleText,
  };
}
"""


@dataclass(slots=True)
class RenderedLink:
    text: str
    href: str


@dataclass(slots=True)
class RenderedField:
    name: str
    field_type: str
    element_id: str = ""
    placeholder: str = ""


@dataclass(slots=True)
class RenderResult:
    png_bytes: bytes
    width: int
    height: int
    output_path: Path | None = None
    broken_images: list[str] = field(default_factory=list)
    unresolved_assets: list[str] = field(default_factory=list)
    external_requests: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    has_horizontal_overflow: bool = False
    links: list[RenderedLink] = field(default_factory=list)
    form_fields: list[RenderedField] = field(default_factory=list)
    visible_text: str = ""

    def to_summary_dict(self, *, max_text_chars: int = 2000) -> dict[str, Any]:
        """Serialize DOM and layout telemetry for structured QA inspection."""
        return {
            "width": self.width,
            "height": self.height,
            "png_bytes_len": len(self.png_bytes),
            "output_path": str(self.output_path) if self.output_path is not None else None,
            "broken_images": list(self.broken_images),
            "unresolved_assets": list(self.unresolved_assets),
            "external_requests": list(self.external_requests),
            "console_errors": list(self.console_errors),
            "has_horizontal_overflow": self.has_horizontal_overflow,
            "links": [{"text": link.text, "href": link.href} for link in self.links],
            "form_fields": [
                {
                    "name": f.name,
                    "type": f.field_type,
                    "id": f.element_id,
                    "placeholder": f.placeholder,
                }
                for f in self.form_fields
            ],
            "visible_text_excerpt": self.visible_text[:max_text_chars],
        }


def _guess_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    explicit = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".gif": "image/gif",
    }
    if suffix in explicit:
        return explicit[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def inline_local_assets(
    html: str,
    asset_map: dict[str, Path] | None = None,
) -> tuple[str, list[str]]:
    """Replace ``asset://<id>`` URIs with inline data URIs when present in ``asset_map``.

    Returns the rewritten HTML and a sorted deduplicated list of unresolved ``asset://...`` URIs.
    """
    normalized: dict[str, Path] = {}
    if asset_map:
        for key, path in asset_map.items():
            clean_key = key.removeprefix("asset://")
            normalized[clean_key] = path

    unresolved: list[str] = []
    seen_unresolved: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        asset_id = match.group(1)
        full_uri = match.group(0)
        local_path = normalized.get(asset_id)
        if local_path is None or not local_path.is_file():
            if full_uri not in seen_unresolved:
                seen_unresolved.add(full_uri)
                unresolved.append(full_uri)
            return full_uri
        raw_bytes = local_path.read_bytes()
        mime = _guess_mime_type(local_path)
        encoded = base64.b64encode(raw_bytes).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    rewritten = ASSET_URI_RE.sub(_replace, html)
    return rewritten, unresolved


class HtmlRenderer:
    """Reusable async Playwright headless Chromium HTML-to-PNG renderer with DOM telemetry."""

    def __init__(self, *, timeout_ms: int = 15_000) -> None:
        self.timeout_ms = timeout_ms
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def start(self) -> None:
        if self._browser is not None:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def render(
        self,
        html: str,
        *,
        mode: RenderMode = "email",
        asset_map: dict[str, Path] | None = None,
        save_path: Path | None = None,
        viewport: tuple[int, int] | None = None,
    ) -> RenderResult:
        """Render HTML offline in headless Chromium and collect PNG bytes plus DOM/layout checks."""
        if self._browser is None:
            await self.start()
        assert self._browser is not None

        vp_width, vp_height = viewport or _VIEWPORTS[mode]
        rewritten_html, unresolved_assets = inline_local_assets(html, asset_map=asset_map)

        external_requests: list[str] = []
        console_errors: list[str] = []

        context = await self._browser.new_context(
            viewport={"width": vp_width, "height": vp_height},
            device_scale_factor=1,
            java_script_enabled=False,
        )
        try:
            page = await context.new_page()

            async def _handle_route(route: Route) -> None:
                url = route.request.url
                if url.startswith(("http://", "https://")):
                    if url not in external_requests:
                        external_requests.append(url)
                    await route.abort()
                    return
                await route.continue_()

            def _on_console(msg: ConsoleMessage) -> None:
                if msg.type == "error":
                    console_errors.append(msg.text)

            def _on_page_error(exc: Exception) -> None:
                console_errors.append(str(exc))

            await page.route("**/*", _handle_route)
            page.on("console", _on_console)
            page.on("pageerror", _on_page_error)

            await page.set_content(
                rewritten_html,
                wait_until="load",
                timeout=self.timeout_ms,
            )

            raw_metrics = cast(dict[str, Any], await page.evaluate(_DOM_INSPECT_SCRIPT))
            png_bytes = await page.screenshot(type="png", full_page=True, timeout=self.timeout_ms)
        finally:
            await context.close()

        out_path: Path | None = None
        if save_path is not None:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(png_bytes)
            out_path = save_path

        links = [
            RenderedLink(text=str(item.get("text", "")), href=str(item.get("href", "")))
            for item in cast(list[dict[str, Any]], raw_metrics.get("links", []))
        ]
        form_fields = [
            RenderedField(
                name=str(item.get("name", "")),
                field_type=str(item.get("field_type", "")),
                element_id=str(item.get("element_id", "")),
                placeholder=str(item.get("placeholder", "")),
            )
            for item in cast(list[dict[str, Any]], raw_metrics.get("form_fields", []))
        ]

        return RenderResult(
            png_bytes=png_bytes,
            width=int(raw_metrics.get("width", vp_width)),
            height=int(raw_metrics.get("height", vp_height)),
            output_path=out_path,
            broken_images=[str(x) for x in raw_metrics.get("broken_images", [])],
            unresolved_assets=unresolved_assets,
            external_requests=external_requests,
            console_errors=console_errors,
            has_horizontal_overflow=bool(raw_metrics.get("has_horizontal_overflow", False)),
            links=links,
            form_fields=form_fields,
            visible_text=str(raw_metrics.get("visible_text", "")),
        )


async def render_html_to_png(
    html: str,
    *,
    mode: RenderMode = "email",
    asset_map: dict[str, Path] | None = None,
    save_path: Path | None = None,
    viewport: tuple[int, int] | None = None,
    timeout_ms: int = 15_000,
) -> RenderResult:
    """One-shot convenience wrapper around ``HtmlRenderer``."""
    async with HtmlRenderer(timeout_ms=timeout_ms) as renderer:
        return await renderer.render(
            html,
            mode=mode,
            asset_map=asset_map,
            save_path=save_path,
            viewport=viewport,
        )
