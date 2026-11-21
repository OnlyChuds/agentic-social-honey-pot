from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from src.models import DecisionRecord, Event, Snapshot
from src.store.db import Store


def build_replay_payload(store: Store) -> dict[str, Any]:
    """Assemble a JSON-serializable summary of a campaign run from SQLite."""
    snapshot: Snapshot | None = store.latest_snapshot()
    events: list[Event] = store.get_events()
    decisions: list[DecisionRecord] = store.get_decisions()
    assets = store.list_assets()

    return {
        "snapshot": snapshot.model_dump(mode="json") if snapshot else None,
        "summary": {
            "state": snapshot.run.state if snapshot else "EMPTY",
            "scenario": snapshot.run.scenario if snapshot else "",
            "target_id": snapshot.target.id if snapshot else "",
            "target_name": snapshot.target.name if snapshot else "",
            "version": snapshot.version if snapshot else 0,
            "touches_used": snapshot.campaign.budget.used if snapshot else 0,
            "touch_budget": snapshot.campaign.budget.session_max if snapshot else 0,
            "sessions_used": snapshot.sessions.used if snapshot else 0,
            "sessions_allowed": snapshot.sessions.allowed if snapshot else 0,
            "facts_count": len(snapshot.facts) if snapshot else 0,
            "hooks_count": len(snapshot.hooks) if snapshot else 0,
            "incidents_count": len(snapshot.incidents) if snapshot else 0,
            "assets_uploaded": len(assets),
            "events_count": len(events),
            "decisions_count": len(decisions),
        },
        "events": [e.model_dump(mode="json") for e in events],
        "decisions": [d.model_dump(mode="json") for d in decisions],
    }


def render_markdown(store: Store) -> str:
    """Render a pitch-ready Markdown campaign timeline from SQLite."""
    data = build_replay_payload(store)
    summary = data["summary"]
    snap = data["snapshot"]
    lines: list[str] = [
        "# Campaign Replay Timeline",
        "",
        "## Summary",
        f"- **State:** `{summary['state']}`",
        f"- **Scenario / Target:** `{summary['scenario']}` / `{summary['target_id']}` ({summary['target_name']})",
        f"- **Touches:** {summary['touches_used']} / {summary['touch_budget']}",
        f"- **Sessions:** {summary['sessions_used']} / {summary['sessions_allowed']}",
        f"- **Facts / Hooks:** {summary['facts_count']} facts, {summary['hooks_count']} hooks",
        f"- **Incidents:** {summary['incidents_count']}",
        f"- **Assets Uploaded:** {summary['assets_uploaded']}",
        "",
    ]

    if snap and snap.get("incidents"):
        lines.append("## Incidents")
        for idx, inc in enumerate(snap["incidents"], start=1):
            lines.append(
                f"{idx}. **{inc.get('objective', 'unknown')}** "
                f"(touch {inc.get('touches_used', '?')}): {inc.get('summary', '')} "
                f"— evidence: `{inc.get('evidence', [])}`"
            )
        lines.append("")

    if snap and snap.get("conversation", {}).get("turns"):
        lines.append("## Conversation Turns")
        for turn in snap["conversation"]["turns"]:
            direction = "OUT" if turn["dir"] == "out" else "IN"
            preview = str(turn["body"]).replace("\n", " ")[:180]
            lines.append(
                f"- **#{turn['n']} [{direction}]** `{turn['channel']}` ({turn['ts']}): {preview}"
            )
        lines.append("")

    lines.append("## Orchestrator Decisions")
    if not data["decisions"]:
        lines.append("_No decisions logged._")
    else:
        for idx, dec in enumerate(data["decisions"], start=1):
            ops = ", ".join(str(a.get("op", "?")) for a in dec.get("actions", [])) or "noop"
            lines.append(
                f"{idx}. **v{dec['snapshot_version']}** `{dec['trigger']}` [{ops}] — {dec.get('notes', '')}"
            )
    lines.append("")
    return "\n".join(lines)


def render_text(store: Store) -> str:
    """Render a compact plain-text timeline for terminal debugging."""
    data = build_replay_payload(store)
    s = data["summary"]
    lines = [
        "=== CAMPAIGN REPLAY ===",
        f"State:    {s['state']}  (v{s['version']})",
        f"Target:   {s['target_id']} ({s['target_name']}) [{s['scenario']}]",
        (
            f"Budget:   {s['touches_used']}/{s['touch_budget']} touches | "
            f"{s['sessions_used']}/{s['sessions_allowed']} sessions"
        ),
        (
            f"Memory:   {s['facts_count']} facts | {s['hooks_count']} hooks | "
            f"{s['incidents_count']} incidents | {s['assets_uploaded']} assets"
        ),
        "",
        "--- Decisions ---",
    ]
    for idx, dec in enumerate(data["decisions"], start=1):
        ops = ", ".join(str(a.get("op", "?")) for a in dec.get("actions", [])) or "noop"
        lines.append(f"[{idx:02d}] v{dec['snapshot_version']:02d} {dec['trigger']:<13} ({ops}) {dec.get('notes', '')}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a campaign SQLite database.")
    parser.add_argument("--db", required=True, help="Path to SQLite DB file")
    parser.add_argument(
        "--format",
        choices=("text", "markdown", "json"),
        default="text",
        help="Output format",
    )
    parser.add_argument("--out", default=None, help="Optional file path to write output")
    args = parser.parse_args()

    with Store(args.db) as store:
        if args.format == "markdown":
            rendered = render_markdown(store)
        elif args.format == "json":
            rendered = json.dumps(build_replay_payload(store), indent=2, ensure_ascii=False)
        else:
            rendered = render_text(store)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
