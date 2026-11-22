#!/usr/bin/env python3
"""Post-batch audit for overnight practice runs.

Inspects campaign SQLite DBs and rendered PNGs to produce:
  1. ``AUDIT.md`` — fact-grounding risk check (-5 pt penalty guard), channel/opener
     win matrix, and ranked pitch-ready thread candidates.
  2. ``gallery.html`` — single-page visual Craft review sheet showing all rendered
     email & landing-page PNGs with their QA scores and issues.

Usage:
    .venv/bin/python scripts/audit_overnight.py --data-dir data --out-dir data/overnight/audit
    .venv/bin/python scripts/audit_overnight.py --dbs data/practice_julius_run1.db data/practice_tom_run1.db
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.summarize_runs import summarize_run


def _inspect_db(db_path: Path) -> dict[str, Any]:
    """Read-only inspection of one campaign DB for fact grounding, channels, QA, and pitch ranking."""
    base = summarize_run(db_path)
    uri = db_path.resolve().as_uri() + "?mode=ro"

    outbound_turns: list[dict[str, Any]] = []
    qa_verdicts: list[dict[str, Any]] = []
    snapshot_data: dict[str, Any] = {}
    session_id = ""

    with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as conn:
        conn.row_factory = sqlite3.Row
        snap_row = conn.execute(
            "SELECT snapshot_json FROM snapshots ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if snap_row is not None:
            snapshot_data = json.loads(snap_row["snapshot_json"])
            session_id = str(snapshot_data.get("sessions", {}).get("current") or "")

        ev_rows = conn.execute(
            "SELECT event_id, ts, type, actor, payload_json FROM events ORDER BY seq ASC"
        ).fetchall()

    facts_by_id: dict[str, dict[str, Any]] = {
        str(f.get("fact_id")): f
        for f in snapshot_data.get("facts", [])
        if isinstance(f, dict) and f.get("fact_id")
    }

    pivots = 0
    for row in ev_rows:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        ev_type = row["type"]
        actor = row["actor"]
        if ev_type == "agent_output" and actor == "messaging" and payload.get("direction") == "out":
            outbound_turns.append(payload)
        elif ev_type == "agent_output" and actor == "qa" and payload.get("kind") == "qa_verdict":
            qa_inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
            qa_verdicts.append(qa_inner)
        elif (
            ev_type == "agent_output"
            and actor == "strategy"
            and payload.get("kind") == "plan"
            and "pivot" in str(payload.get("rationale", "")).lower()
        ):
            pivots += 1

    # Fact-grounding risk check
    fact_risks: list[str] = []
    for idx, turn in enumerate(outbound_turns, start=1):
        used_ids = [str(fid) for fid in (turn.get("facts_used") or [])]
        if idx == 1 and not used_ids and facts_by_id:
            fact_risks.append("touch 1 cited 0 facts_used (check whether claims were ungrounded)")
        for fid in used_ids:
            fobj = facts_by_id.get(fid)
            if fobj is None:
                fact_risks.append(f"touch {idx} cited unknown fact_id '{fid}'")
                continue
            if idx == 1 and fobj.get("provenance") == "inferred" and float(fobj.get("confidence", 0.0)) < 0.5:
                fact_risks.append(f"touch 1 cited low-confidence inferred fact '{fid}'")

    # Craft score median across QA verdicts
    craft_totals: list[int] = []
    for v in qa_verdicts:
        scores = v.get("scores")
        if isinstance(scores, dict) and scores:
            vals = [int(x) for x in scores.values() if isinstance(x, (int, float))]
            if vals:
                craft_totals.append(sum(vals))

    opener_channel = str(outbound_turns[0].get("channel") or "none") if outbound_turns else "none"
    target = snapshot_data.get("target") or {}
    incidents = snapshot_data.get("incidents") or []
    first_objective = str(incidents[0].get("objective") or "") if incidents else ""

    return {
        "run_id": db_path.stem,
        "db_path": str(db_path),
        "session_id": session_id,
        "target_id": str(target.get("id") or db_path.stem),
        "target_name": str(target.get("name") or ""),
        "state": base.get("last_recorded_state") or "UNKNOWN",
        "exit_reason": base.get("exit_reason"),
        "touches": int(base.get("current_session_touches") or 0),
        "replies": int(base.get("replies") or 0),
        "qa_checks": int(base.get("qa_checks") or 0),
        "qa_failures": int(base.get("qa_failures") or 0),
        "incidents": int(base.get("incident_records") or 0),
        "objective": first_objective,
        "opener_channel": opener_channel,
        "pivots": pivots,
        "facts_count": len(facts_by_id),
        "fact_risks": fact_risks,
        "median_craft_8": round(statistics.median(craft_totals), 1) if craft_totals else None,
        "qa_verdicts": qa_verdicts,
        "error_categories": base.get("error_categories") or {},
    }


def _collect_renders(renders_dir: Path, session_ids: set[str] | None = None) -> list[Path]:
    if not renders_dir.is_dir():
        return []
    pngs = sorted(renders_dir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    if session_ids:
        filtered = [p for p in pngs if any(p.name.startswith(f"{sid}_") for sid in session_ids if sid)]
        if filtered:
            return filtered
    return pngs[:60]


def render_audit_markdown(inspections: list[dict[str, Any]], *, gallery_rel: str | None = None) -> str:
    lines: list[str] = [
        "# Overnight Campaign Audit",
        "",
        f"- **Runs audited:** `{len(inspections)}`",
        f"- **Wins:** `{sum(1 for x in inspections if x['state'] == 'WIN')}/{len(inspections)}`",
        f"- **Runs with fact-grounding warnings:** `{sum(1 for x in inspections if x['fact_risks'])}`",
    ]
    if gallery_rel:
        lines.append(f"- **Visual Craft Gallery:** [{gallery_rel}]({gallery_rel})")
    lines.append("")

    # 1. Best Pitch Thread Candidates
    def _pitch_key(item: dict[str, Any]) -> tuple[int, int, float, int]:
        is_win = 1 if item["state"] == "WIN" else 0
        no_risk = 1 if not item["fact_risks"] else 0
        craft = float(item["median_craft_8"] or 0.0)
        neg_touches = -(item["touches"] or 99)
        return (is_win, no_risk, craft, neg_touches)

    ranked = sorted(inspections, key=_pitch_key, reverse=True)
    lines.extend(
        [
            "## Top Pitch Walkthrough Candidates",
            "",
            "| Rank | Run ID | Target | State | Objective | Touches | Replies | Pivots | Craft (/8) | Fact Risks |",
            "|---:|---|---|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for rank, item in enumerate(ranked[:10], start=1):
        craft_s = f"{item['median_craft_8']:.1f}" if item["median_craft_8"] is not None else "n/a"
        lines.append(
            f"| {rank} | `{item['run_id']}` | `{item['target_id']}` | **{item['state']}** "
            f"| `{item['objective'] or '—'}` | {item['touches']} | {item['replies']} "
            f"| {item['pivots']} | {craft_s} | {len(item['fact_risks'])} |"
        )
    lines.append("")

    # 2. Channel / Opener Win Matrix
    by_channel: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in inspections:
        by_channel[item["opener_channel"]].append(item)

    lines.extend(
        [
            "## Opener Channel Performance",
            "",
            "| Opener Channel | Runs | Wins | Reply Rate | Median Touches (WIN) |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for ch, grp in sorted(by_channel.items()):
        n = len(grp)
        w = sum(1 for x in grp if x["state"] == "WIN")
        with_reply = sum(1 for x in grp if x["replies"] > 0)
        win_t = [x["touches"] for x in grp if x["state"] == "WIN" and x["touches"] > 0]
        med_t = f"{statistics.median(win_t):.1f}" if win_t else "n/a"
        lines.append(f"| `{ch}` | {n} | {w}/{n} | {100.0 * with_reply / n:.0f}% | {med_t} |")
    lines.append("")

    # 3. Fact-Grounding Risk Findings (-5 pt penalty protection)
    lines.append("## Fact-Grounding Warnings")
    lines.append("")
    risk_rows = [item for item in inspections if item["fact_risks"]]
    if not risk_rows:
        lines.append("- None detected across audited runs.")
    else:
        for item in risk_rows:
            for risk in item["fact_risks"]:
                lines.append(f"- `{item['run_id']}`: {risk}")
    lines.append("")

    # 4. Error categories aggregate
    err_counts: Counter[str] = Counter()
    for item in inspections:
        for k, v in item["error_categories"].items():
            err_counts[k] += int(v)
    if err_counts:
        lines.append("## Aggregate Error Categories")
        lines.append("")
        for k, v in err_counts.most_common():
            lines.append(f"- `{k}`: {v}")
        lines.append("")

    return "\n".join(lines)


def render_gallery_html(render_paths: list[Path], *, out_dir: Path) -> str:
    cards: list[str] = []
    for png in render_paths:
        rel = os.path.relpath(png.resolve(), out_dir.resolve()).replace("\\", "/")
        safe_name = html_lib.escape(png.name)
        safe_rel = html_lib.escape(rel, quote=True)
        kind = "Landing Page" if "_page_" in png.name else "Email"
        cards.append(
            f'<div class="card"><div class="meta"><strong>{safe_name}</strong>'
            f'<span>{kind}</span></div>'
            f'<a href="{safe_rel}" target="_blank"><img src="{safe_rel}" alt="{safe_name}" loading="lazy"></a></div>'
        )
    grid = "\n".join(cards) if cards else '<p class="empty">No rendered PNGs found yet.</p>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Craft Visual Audit Gallery</title>
<style>
body{{margin:0;padding:24px;background:#09090b;color:#fafafa;font-family:Inter,system-ui,sans-serif;}}
h1{{margin:0 0 8px;font-size:20px;}}
p.sub{{margin:0 0 20px;color:#a1a1aa;font-size:13px;}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px;}}
.card{{background:#111114;border:1px solid #27272a;border-radius:10px;overflow:hidden;}}
.meta{{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid #27272a;font-size:12px;font-family:ui-monospace,monospace;color:#e4e4e7;}}
.meta span{{color:#34d399;}}
img{{display:block;width:100%;height:auto;background:#ffffff;}}
.empty{{color:#a1a1aa;}}
</style>
</head>
<body>
<h1>Overnight Craft Visual Gallery</h1>
<p class="sub">{len(render_paths)} rendered email and landing-page screenshots (click any card to open full resolution).</p>
<div class="grid">{grid}</div>
</body>
</html>
"""


def audit_dbs(
    db_paths: list[Path],
    *,
    out_dir: Path,
    renders_dir: Path | None = None,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    inspections = [_inspect_db(p) for p in db_paths if p.is_file()]
    session_ids = {x["session_id"] for x in inspections if x.get("session_id")}

    gallery_path = out_dir / "gallery.html"
    resolved_renders = renders_dir if renders_dir is not None else (db_paths[0].parent / "renders" if db_paths else Path("data/renders"))
    pngs = _collect_renders(resolved_renders, session_ids=session_ids or None)
    gallery_path.write_text(render_gallery_html(pngs, out_dir=out_dir), encoding="utf-8")

    audit_path = out_dir / "AUDIT.md"
    audit_path.write_text(
        render_audit_markdown(inspections, gallery_rel=gallery_path.name),
        encoding="utf-8",
    )
    return audit_path, gallery_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit overnight practice runs and generate Craft gallery.")
    parser.add_argument("--data-dir", default="data", help="Directory containing *.db and renders/")
    parser.add_argument("--out-dir", default="data/overnight/audit", help="Output directory for AUDIT.md + gallery.html")
    parser.add_argument("--dbs", nargs="*", default=None, help="Explicit list of .db files to audit")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    if args.dbs:
        db_paths = [Path(p) for p in args.dbs]
    else:
        db_paths = sorted(
            [p for p in data_dir.glob("practice_*.db") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    audit_path, gallery_path = audit_dbs(
        db_paths,
        out_dir=Path(args.out_dir),
        renders_dir=data_dir / "renders",
    )
    print(f"Audit written:   {audit_path}")
    print(f"Gallery written: {gallery_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
