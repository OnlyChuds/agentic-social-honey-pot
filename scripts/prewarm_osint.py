"""Pre-warm data/osint_cache/ in parallel for all targets in a Target API scenario.

Usage:
    uv run python scripts/prewarm_osint.py --scenario practice
    uv run python scripts/prewarm_osint.py --scenario eval --force
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Any

import httpx

from src.agents.osint import OSINTAgent
from src.config import load_config
from src.llm.text import TextLLMClient
from src.models import RunMeta, Snapshot, TargetInfo

ROOT = Path(__file__).resolve().parent.parent


async def fetch_scenario_targets(
    base_url: str,
    team_key: str,
    scenario_id: str,
) -> tuple[list[dict[str, Any]], str]:
    async with httpx.AsyncClient(
        timeout=30.0,
        headers={"Authorization": f"Bearer {team_key}"},
    ) as http:
        resp = await http.get(f"{base_url.rstrip('/')}/scenarios")
        resp.raise_for_status()
        for sc in resp.json():
            if sc.get("id") == scenario_id:
                targets = [t for t in sc.get("targets", []) if isinstance(t, dict)]
                return targets, str(sc.get("company") or "")
    return [], ""


async def _warm_one(
    agent: OSINTAgent,
    *,
    scenario: str,
    company: str,
    target_dict: dict[str, Any],
    force: bool,
) -> tuple[str, str, float]:
    t0 = time.monotonic()
    tid = str(target_dict.get("id") or "")
    snap = Snapshot(
        run=RunMeta(scenario="eval" if scenario == "eval" else "practice", target_id=tid),
        target=TargetInfo(
            id=tid,
            name=str(target_dict.get("name") or tid),
            role=str(target_dict.get("role") or ""),
            org=company,
        ),
    )
    prop = await agent.run(snap, mode="bootstrap", use_cache=not force)
    return tid, prop.rationale, time.monotonic() - t0


async def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-warm OSINT cache for all scenario targets.")
    parser.add_argument("--scenario", choices=("practice", "eval"), default="practice")
    parser.add_argument("--force", action="store_true", help="Ignore existing cache and re-scrape")
    args = parser.parse_args()

    cfg = load_config(ROOT / "config.yaml", ROOT / ".env")
    targets, company = await fetch_scenario_targets(
        cfg.target_api.base_url,
        cfg.target_api.team_key,
        args.scenario,
    )
    if not targets:
        print(f"No targets found for scenario={args.scenario!r}.")
        return 1

    print(
        f"Pre-warming OSINT cache for {len(targets)} {args.scenario} targets "
        f"@ {company or 'unknown'} (force={args.force})..."
    )
    llm = TextLLMClient.from_config(cfg.models.text)
    agent = OSINTAgent(llm, config=cfg)
    t0 = time.monotonic()
    try:
        results = await asyncio.gather(
            *[
                _warm_one(
                    agent,
                    scenario=args.scenario,
                    company=company,
                    target_dict=t,
                    force=args.force,
                )
                for t in targets
            ],
            return_exceptions=True,
        )
    finally:
        await llm.aclose()

    failures = 0
    for item in results:
        if isinstance(item, BaseException):
            failures += 1
            print(f"  [ERR] {type(item).__name__}: {item}")
        else:
            tid, rationale, dt = item
            print(f"  [OK {dt:5.1f}s] {tid}: {rationale}")
    print(f"Finished in {time.monotonic() - t0:.1f}s ({len(targets) - failures}/{len(targets)} ok).")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
