#!/usr/bin/env python3
"""Pre-flight health check for live practice / eval readiness.

Verifies in ~10s:
  1. Target API reachability + TEAM_API_KEY auth + open scenarios (/v1/scenarios)
  2. Text LLM endpoint reachability + JSON-mode latency & tokens/sec
  3. ComfyUI image server reachability (/system_stats)
  4. Playwright headless Chromium launch + minimal HTML render
  5. OSINT provider env keys configured (Jina / Exa / Tavily / Apify)

Usage:
    .venv/bin/python scripts/preflight.py
    .venv/bin/python scripts/preflight.py --offline   # skip remote network checks
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import AppConfig, load_config
from src.llm.text import TextLLMClient
from src.osint import OsintEngine
from src.render.screenshot import HtmlRenderer


@dataclass(slots=True)
class CheckResult:
    name: str
    ok: bool
    elapsed_ms: int
    detail: str
    required: bool = True


class _PingPayload(BaseModel):
    ok: bool


async def check_target_api(cfg: AppConfig) -> CheckResult:
    t0 = time.monotonic()
    url = cfg.target_api.base_url.rstrip("/")
    key = cfg.target_api.team_key.strip()
    if not url or not key:
        return CheckResult(
            name="target_api",
            ok=False,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            detail="Missing TARGET_API_URL or TEAM_API_KEY in .env/config.yaml",
        )
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{url}/scenarios",
                headers={"Authorization": f"Bearer {key}"},
            )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code != 200:
            return CheckResult(
                name="target_api",
                ok=False,
                elapsed_ms=elapsed_ms,
                detail=f"HTTP {resp.status_code}: {resp.text[:160]}",
            )
        data = resp.json()
        scenarios = [
            f"{s.get('id')}({'open' if s.get('open', True) else 'closed'})"
            for s in (data if isinstance(data, list) else [])
            if isinstance(s, dict)
        ]
        return CheckResult(
            name="target_api",
            ok=True,
            elapsed_ms=elapsed_ms,
            detail=f"200 OK · scenarios: {', '.join(scenarios) or 'none'}",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="target_api",
            ok=False,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            detail=f"{type(exc).__name__}: {exc}",
        )


async def check_text_llm(cfg: AppConfig) -> CheckResult:
    t0 = time.monotonic()
    if not cfg.models.text.base_url:
        return CheckResult(
            name="text_llm",
            ok=False,
            elapsed_ms=0,
            detail="Missing LLM_BASE_URL",
        )
    llm = TextLLMClient.from_config(cfg.models.text, timeout_s=30.0)
    try:
        comp = await llm.complete(
            [
                {"role": "system", "content": "Return compact minified JSON only."},
                {"role": "user", "content": 'Return {"ok":true}'},
            ],
            temperature=0.0,
            max_tokens=32,
            json_mode=True,
        )
        elapsed_s = max(0.001, time.monotonic() - t0)
        parsed = _PingPayload.model_validate_json(comp.content.strip())
        tok_s = comp.completion_tokens / elapsed_s if comp.completion_tokens else 0.0
        detail = (
            f"model={llm.model} · ok={parsed.ok} · "
            f"{comp.completion_tokens} tok in {elapsed_s:.2f}s"
            + (f" ({tok_s:.1f} tok/s)" if tok_s > 0 else "")
        )
        return CheckResult(
            name="text_llm",
            ok=parsed.ok,
            elapsed_ms=int(elapsed_s * 1000),
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="text_llm",
            ok=False,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            detail=f"{type(exc).__name__}: {exc}",
        )
    finally:
        await llm.aclose()


async def check_image_server(cfg: AppConfig) -> CheckResult:
    t0 = time.monotonic()
    base = cfg.models.image.base_url.rstrip("/")
    if not base:
        return CheckResult(
            name="image_comfyui",
            ok=False,
            elapsed_ms=0,
            detail="IMAGE_BASE_URL not configured (portraits/logos will be skipped)",
            required=False,
        )
    headers = (
        {"Authorization": f"Bearer {cfg.models.image.api_key}"}
        if cfg.models.image.api_key
        else {}
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base}/system_stats", headers=headers)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code != 200:
            return CheckResult(
                name="image_comfyui",
                ok=False,
                elapsed_ms=elapsed_ms,
                detail=f"HTTP {resp.status_code}: {resp.text[:140]}",
                required=False,
            )
        return CheckResult(
            name="image_comfyui",
            ok=True,
            elapsed_ms=elapsed_ms,
            detail="200 OK · ComfyUI /system_stats reachable",
            required=False,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="image_comfyui",
            ok=False,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            detail=f"{type(exc).__name__}: {exc}",
            required=False,
        )


async def check_playwright() -> CheckResult:
    t0 = time.monotonic()
    try:
        async with HtmlRenderer() as renderer:
            res = await renderer.render(
                "<!doctype html><html><body><h1>Preflight OK</h1></body></html>",
                mode="email",
            )
        ok = res.png_bytes.startswith(b"\x89PNG") and not res.has_horizontal_overflow
        return CheckResult(
            name="playwright",
            ok=ok,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            detail=f"rendered {len(res.png_bytes)}B PNG ({res.width}x{res.height})",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="playwright",
            ok=False,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            detail=f"{type(exc).__name__}: {exc}",
        )


def check_osint_keys() -> CheckResult:
    t0 = time.monotonic()
    engine = OsintEngine.from_env()
    provs = engine.available_providers()
    return CheckResult(
        name="osint_providers",
        ok=len(provs) > 0,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        detail=f"configured: {', '.join(provs)}" if provs else "none configured (thin-OSINT fallback only)",
        required=False,
    )


async def run_preflight(
    *,
    config_path: str | Path = "config.yaml",
    offline: bool = False,
) -> list[CheckResult]:
    cfg = load_config(config_path)
    results: list[CheckResult] = [check_osint_keys()]
    tasks = [check_playwright()]
    if not offline:
        tasks.extend(
            [
                check_target_api(cfg),
                check_text_llm(cfg),
                check_image_server(cfg),
            ]
        )
    results.extend(await asyncio.gather(*tasks))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-flight readiness check for practice/eval runs.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--offline", action="store_true", help="Skip remote HTTP checks (Playwright + env only)")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)

    results = asyncio.run(run_preflight(config_path=args.config, offline=args.offline))
    required_ok = all(r.ok for r in results if r.required)

    if args.json:
        payload: dict[str, Any] = {
            "ready": required_ok,
            "checks": [asdict(r) for r in results],
        }
        print(json.dumps(payload, indent=2))
    else:
        for r in results:
            badge = "PASS" if r.ok else ("FAIL" if r.required else "WARN")
            print(f"[{badge}] {r.name:<16} ({r.elapsed_ms:>5} ms)  {r.detail}")
        print(f"\nOverall pre-flight ready: {'YES' if required_ok else 'NO'}")
    return 0 if required_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
