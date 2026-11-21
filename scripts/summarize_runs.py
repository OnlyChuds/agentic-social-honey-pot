"""Summarize saved run metadata offline without exporting conversation contents."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

TERMINAL_STATES = {"WIN", "ABORT", "DONE"}
KNOWN_STATES = TERMINAL_STATES | {"BOOT", "RESEARCH", "ENGAGE"}
EXIT_REASONS = {"completed", "cycle_limit", "operator_stop", "service_failure", "unknown"}
ERROR_CATEGORIES = {"timeout", "connection", "validation", "unknown"}
_HTTP_CATEGORY = re.compile(r"http_[45][0-9]{2}")
_LEGACY_HTTP = re.compile(
    r"^\s*(?:[\w.]+(?:Error|Exception):\s*)?"
    r"(?:Error code:\s*|HTTP(?: status(?: code)?)?\s+|status(?: code)?:\s*)"
    r"([45][0-9]{2})\b",
    re.IGNORECASE,
)
_LEGACY_TIMEOUT = re.compile(
    r"^\s*(?:[\w.]*Timeout(?:Error|Exception)?(?=:|$)|(?:Request )?timed out(?=[.!:]|$))",
    re.IGNORECASE,
)


def _elapsed(start: str | None, end: str | None) -> float | None:
    if start is None or end is None:
        return None
    return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 3)


def _error_category(explicit: object, legacy: object) -> str:
    if explicit is not None:
        if isinstance(explicit, str) and (
            explicit in ERROR_CATEGORIES or _HTTP_CATEGORY.fullmatch(explicit)
        ):
            return explicit
        return "unknown"
    if isinstance(legacy, str):
        status = _LEGACY_HTTP.match(legacy)
        if status:
            return f"http_{status[1]}"
        if _LEGACY_TIMEOUT.match(legacy):
            return "timeout"
    return "unknown"


def summarize_run(path: Path) -> dict[str, Any]:
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        # Read one consistent SQLite view, including committed records still in the WAL.
        db.execute("BEGIN")
        event_stats = db.execute(
            """
            WITH labels AS (
                SELECT ts, type, actor,
                       json_extract(payload_json, '$.kind') AS kind,
                       type = 'agent_output' AND actor = 'messaging'
                           AND json_extract(payload_json, '$.direction') = 'out' AS outbound,
                       type = 'agent_output' AND actor = 'qa'
                           AND json_extract(payload_json, '$.kind') = 'qa_verdict' AS qa_check,
                       json_type(payload_json, '$.payload.pass') = 'false' AS qa_failed,
                       json_extract(payload_json, '$.event') AS event_name,
                       json_extract(payload_json, '$.action') AS action
                FROM events
            )
            SELECT min(ts) AS first_event_at, max(ts) AS last_event_at,
                   min(ts) FILTER (WHERE type = 'session' AND action = 'started')
                       AS first_session_started_at,
                   min(ts) FILTER (WHERE outbound) AS first_outbound_at,
                   count(*) FILTER (WHERE outbound) AS outbound_turns,
                   count(*) FILTER (WHERE type = 'target_event') AS target_events,
                   count(*) FILTER (WHERE type = 'target_event' AND event_name = 'reply') AS replies,
                   count(*) FILTER (WHERE qa_check) AS qa_checks,
                   count(*) FILTER (WHERE qa_check AND qa_failed) AS qa_failures,
                   count(*) FILTER (WHERE type = 'incident') AS incident_records
            FROM labels
            """
        ).fetchone()
        snapshot = db.execute(
            """
            SELECT ts, state, json_extract(snapshot_json, '$.campaign.budget.used') AS touches
            FROM snapshots ORDER BY version DESC LIMIT 1
            """
        ).fetchone()
        exit_marker = db.execute(
            """
            SELECT CASE
                       WHEN type = 'system' AND actor IN ('orchestrator', 'system')
                            AND json_extract(payload_json, '$.kind') = 'run_exit'
                       THEN json_extract(payload_json, '$.reason')
                   END AS exit_reason
            FROM events ORDER BY seq DESC LIMIT 1
            """
        ).fetchone()
        errors = Counter(
            _error_category(explicit, legacy)
            for explicit, legacy in db.execute(
                """
                SELECT json_extract(payload_json, '$.category'),
                       json_extract(payload_json, '$.error')
                FROM events
                WHERE type = 'system' AND json_extract(payload_json, '$.kind') = 'agent_error'
                """
            )
        )

    stats = dict(event_stats)
    state = snapshot["state"] if snapshot and snapshot["state"] in KNOWN_STATES else None
    exit_reason = exit_marker["exit_reason"] if exit_marker else None
    return {
        "database": path.name,
        "last_recorded_state": state,
        "terminal_state_recorded": state in TERMINAL_STATES,
        "exit_reason": exit_reason if exit_reason in EXIT_REASONS else None,
        "latest_snapshot_at": snapshot["ts"] if snapshot else None,
        "current_session_touches": snapshot["touches"] if snapshot else None,
        "error_records": sum(errors.values()),
        "error_categories": dict(sorted(errors.items())),
        **stats,
        "observed_event_span_seconds": _elapsed(stats["first_event_at"], stats["last_event_at"]),
        "session_start_to_first_outbound_seconds": _elapsed(
            stats["first_session_started_at"], stats["first_outbound_at"]
        ),
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", nargs="+", type=Path, help="Saved SQLite run database(s)")
    args = parser.parse_args(argv)
    summaries = []
    for path in args.database:
        try:
            summaries.append(summarize_run(path))
        except (sqlite3.Error, ValueError) as exc:
            parser.exit(1, f"{path.name}: could not summarize run ({type(exc).__name__})\n")
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
