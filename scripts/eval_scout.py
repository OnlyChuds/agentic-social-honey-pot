#!/usr/bin/env python3
"""Zero-touch Eval Target Scout: parallel OSINT across all scenario targets before Touch 1.

Calls ``GET /v1/scenarios`` (never creates a session or spends a touch), runs
``OSINTAgent`` in parallel for every target in the chosen scenario to warm
``data/osint_cache/``, ranks targets by OSINT signal depth, and prints a
3-run assignment recommendation (``spread`` vs ``concentrate``).

Usage:
    .venv/bin/python scripts/eval_scout.py --scenario eval
    .venv/bin/python scripts/eval_scout.py --scenario practice --mode spread
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agents.osint import OSINTAgent
from src.api.client import TargetAPIClient
from src.config import AppConfig, load_config
from src.llm.text import TextLLMClient
from src.models import RunMeta, Snapshot, TargetInfo


@dataclass(slots=True)
class ScoutTargetScore:
    target_id: str
    name: str
    role: str
    org: str
    completeness: str
    facts_count: int
    grade_a_b_facts: int
    hooks_count: int
    real_domain: str
    score: float
    rationale: str


def score_dossier_payload(
    *,
    target: TargetInfo,
    payload: dict[str, Any],
    rationale: str = "",
) -> ScoutTargetScore:
    facts = payload.get("facts") or []
    hooks = payload.get("hook_seeds") or []
    completeness = str(payload.get("completeness") or "thin")
    org_branding = payload.get("org_branding") or {}
    real_domain = str(org_branding.get("real_domain") or "")

    grade_ab = sum(
        1
        for f in facts
        if isinstance(f, dict)
        and str(f.get("osint_grade") or "").upper() in {"A", "B"}
        and str(f.get("source") or "") != "scenario:target_metadata"
    )
    comp_bonus = {"full": 3.0, "partial": 1.5, "thin": 0.0}.get(completeness, 0.0)
    score = round(comp_bonus + grade_ab * 1.2 + len(hooks) * 0.8 + (0.5 if real_domain else 0.0), 2)
    return ScoutTargetScore(
        target_id=target.id,
        name=target.name,
        role=target.role,
        org=target.org,
        completeness=completeness,
        facts_count=len(facts),
        grade_a_b_facts=grade_ab,
        hooks_count=len(hooks),
        real_domain=real_domain,
        score=score,
        rationale=rationale,
    )


def recommend_run_assignments(
    ranked: list[ScoutTargetScore],
    *,
    num_runs: int = 3,
    mode: Literal["spread", "concentrate"] = "spread",
) -> list[str]:
    """Recommend target_ids for the ``num_runs`` eval runs."""
    if not ranked or num_runs <= 0:
        return []
    if mode == "concentrate":
        return [ranked[0].target_id] * num_runs

    # Spread across distinct strong targets first; fall back to top-ranked when others are thin
    strong = [r for r in ranked if r.completeness != "thin" and r.score > 1.5]
    pool = strong if strong else ranked
    return [pool[i % len(pool)].target_id for i in range(num_runs)]


async def scout_scenario_targets(
    *,
    cfg: AppConfig,
    scenario_id: str,
    api: TargetAPIClient,
    osint_agent: OSINTAgent,
    mode: Literal["spread", "concentrate"] = "spread",
    num_runs: int = 3,
) -> dict[str, Any]:
    scenarios = await api.get_scenarios()
    matched = next((s for s in scenarios if s.get("id") == scenario_id), None)
    if matched is None:
        raise ValueError(f"Scenario {scenario_id!r} not found in /v1/scenarios")

    company = str(matched.get("company") or "")
    raw_targets = [t for t in matched.get("targets", []) if isinstance(t, dict)]
    targets = [
        TargetInfo(
            id=str(t.get("id") or ""),
            name=str(t.get("name") or t.get("id") or ""),
            role=str(t.get("role") or ""),
            org=company,
        )
        for t in raw_targets
        if t.get("id")
    ]

    async def _scout_one(target: TargetInfo) -> ScoutTargetScore:
        snap = Snapshot(
            version=1,
            run=RunMeta(scenario="eval" if scenario_id == "eval" else "practice", target_id=target.id),
            target=target,
        )
        prop = await osint_agent.run(snap, mode="bootstrap")
        return score_dossier_payload(target=target, payload=prop.payload, rationale=prop.rationale)

    scores = list(await asyncio.gather(*(_scout_one(t) for t in targets)))
    scores.sort(key=lambda s: (s.score, s.grade_a_b_facts, s.hooks_count), reverse=True)
    recommended = recommend_run_assignments(scores, num_runs=num_runs, mode=mode)
    return {
        "scenario": scenario_id,
        "company": company,
        "mode": mode,
        "targets_scouted": len(scores),
        "recommended_runs": recommended,
        "rankings": [asdict(s) for s in scores],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zero-touch OSINT scout across all scenario targets.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--scenario", choices=("practice", "eval"), default="eval")
    parser.add_argument("--mode", choices=("spread", "concentrate"), default="spread")
    parser.add_argument("--num-runs", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)

    async def _run() -> dict[str, Any]:
        llm = TextLLMClient.from_config(cfg.models.text)
        try:
            agent = OSINTAgent(llm, config=cfg)
            async with TargetAPIClient.from_config(cfg.target_api) as api:
                return await scout_scenario_targets(
                    cfg=cfg,
                    scenario_id=args.scenario,
                    api=api,
                    osint_agent=agent,
                    mode=args.mode,
                    num_runs=args.num_runs,
                )
        finally:
            await llm.aclose()

    report = asyncio.run(_run())
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"=== Eval Scout ({report['scenario']} · {report['company']}) ===")
        for idx, row in enumerate(report["rankings"], start=1):
            print(
                f"{idx}. {row['target_id']:<14} score={row['score']:<5} "
                f"comp={row['completeness']:<7} A/B={row['grade_a_b_facts']} "
                f"hooks={row['hooks_count']} domain={row['real_domain'] or '-'}"
            )
        print(f"\nRecommended 3-run plan ({report['mode']}): {report['recommended_runs']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
