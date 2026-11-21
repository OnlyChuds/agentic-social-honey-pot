"""Live smoke test: run OSINTAgent against a real person from the revel8 Target API.

Usage:
    uv run python scripts/test_osint_live.py [target_id] [--rounds N] [--focus "..."]
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

from src.agents.osint import OSINTAgent
from src.config import load_config
from src.llm.text import TextLLMClient
from src.models import RunMeta, Snapshot, TargetInfo
from src.osint import OsintEngine

ROOT = Path(__file__).resolve().parent.parent

PIPELINE_BUDGET_S = 120.0


async def fetch_practice_targets(base_url: str, team_key: str) -> tuple[dict[str, dict[str, str]], str]:
    async with httpx.AsyncClient(
        timeout=30, headers={"Authorization": f"Bearer {team_key}"}
    ) as http:
        r = await http.get(f"{base_url.rstrip('/')}/scenarios")
        r.raise_for_status()
        for sc in r.json():
            if sc.get("id") == "practice":
                targets = {t["id"]: t for t in sc.get("targets", [])}
                return targets, str(sc.get("company", ""))
    return {}, ""


def print_timing_report(agent: OSINTAgent, wall_s: float) -> None:
    result = agent.last_result
    print("\n=== TIMING ===")
    if result is None:
        print(f"  total: {wall_s:.1f}s")
        return
    print(f"  burst1 (searches):   {result.timings.get('burst1_s', 0.0):.1f}s")
    print(f"  burst2 (li+reads):   {result.timings.get('burst2_s', 0.0):.1f}s")
    print(f"  burst3 (follow-ups): {result.timings.get('burst3_s', 0.0):.1f}s")
    print(f"  bursts total:        {result.timings.get('bursts_s', 0.0):.1f}s")
    synth_s = max(0.0, result.total_s - result.timings.get("bursts_s", 0.0))
    print(f"  llm synthesis:       {synth_s:.1f}s")
    print(f"  TOTAL:               {result.total_s:.1f}s (wall {wall_s:.1f}s) budget={result.budget_usd:.3f}usd")
    by_provider: Counter[str] = Counter()
    for s in result.sources:
        by_provider[f"{s.provider}:{s.kind}{'(ok)' if s.ok else '(err)'}"] += 1
    for key, n in sorted(by_provider.items()):
        print(f"    {key}: {n}")
    if result.errors:
        print(f"  errors: {'; '.join(result.errors[:5])}")
    ok = result.total_s <= PIPELINE_BUDGET_S
    print(f"  {'PASS' if ok else 'FAIL'} within {PIPELINE_BUDGET_S:.0f}s pipeline budget")


async def main(target_id: str, max_rounds: int, focus: str | None) -> int:
    cfg = load_config(ROOT / "config.yaml", ROOT / ".env")
    targets, company = await fetch_practice_targets(cfg.target_api.base_url, cfg.target_api.team_key)
    if not targets:
        print("No practice targets found from Target API.")
        return 1
    if target_id not in targets:
        print(f"Target {target_id!r} not found. Available: {', '.join(targets)}")
        return 1

    t = targets[target_id]
    snapshot = Snapshot(
        run=RunMeta(scenario="practice", target_id=target_id),
        target=TargetInfo(id=t["id"], name=t["name"], role=t.get("role", ""), org=company),
    )
    engine = OsintEngine.from_env()
    print(f"Target: {t['name']} ({t.get('role', '')}) @ {company}")
    print(f"Providers live: {', '.join(engine.available_providers()) or 'NONE'}")

    llm = TextLLMClient.from_config(cfg.models.text)
    agent = OSINTAgent(llm, config=cfg)
    t0 = time.monotonic()
    try:
        proposal = await agent.run(snapshot, max_rounds=max_rounds, focus_question=focus)
    finally:
        await llm.aclose()
    wall_s = time.monotonic() - t0

    d = proposal.payload
    print(f"\n=== OSINT PROPOSAL {proposal.proposal_id} ===")
    print(f"rationale: {proposal.rationale}")

    print(f"\n-- facts ({len(d['facts'])}) --")
    for f in d["facts"]:
        grade = f.get("osint_grade") or "-"
        print(f"  [{grade} conf={f.get('confidence')}] ({f.get('category')}) {f.get('claim')}")
        print(f"      src: {f.get('source')}")

    print(f"\n-- hook seeds ({len(d['hook_seeds'])}) --")
    for h in d["hook_seeds"]:
        print(f"  [conf={h.get('confidence')}] {h.get('topic')}")
        print(f"      why: {h.get('why_leverage')}")

    print(f"\n-- psychoprofile ({len(d['psychoprofile'])}) --")
    for p in d["psychoprofile"]:
        print(f"  [conf={p.get('confidence')}] {p.get('trait')}")
        print(f"      ev: {p.get('evidence')}")

    print("\n-- org_branding --")
    print(f"  {json.dumps(d.get('org_branding', {}), ensure_ascii=False)}")

    print("\n-- open_questions --")
    for q in d.get("open_questions", []):
        print(f"  - {q}")

    print(
        f"\nbudget_spent_usd={d.get('budget_spent_usd')} "
        f"escalation_level={d.get('escalation_level')} "
        f"completeness={d.get('completeness')}"
    )
    print_timing_report(agent, wall_s)
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    tid: str | None = None
    rounds = 8
    focus_q: str | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--rounds":
            i += 1
            rounds = int(argv[i])
        elif a == "--focus":
            i += 1
            focus_q = argv[i]
        elif not a.startswith("--") and tid is None:
            tid = a
        i += 1
    asyncio.run(main(tid or "julius", rounds, focus_q))
