#!/usr/bin/env python3
"""Overnight practice batch runner with per-run timeouts, replays, and a morning triage report.

Strictly locked to ``--scenario practice`` (never touches ``eval`` sessions).
Writes campaign SQLite DBs to ``--data-dir`` (default: ``data/``) so they appear
immediately in the dashboard, while storing batch logs, Markdown timelines, and
an incrementally updated ``SUMMARY.md`` under ``data/overnight/<batch_id>/``.

Examples:
    # Quick 5-second smoke test using stub agents + local mock API:
    .venv/bin/python scripts/run_overnight.py --dry-run-stub --rounds 1 --targets julius,tom --cooldown-s 0

    # Real overnight run (3 rounds across julius/tom/robert, up to 6 hours, 15 min cap/run):
    nohup .venv/bin/python scripts/run_overnight.py --rounds 3 --max-hours 6 > overnight.out 2>&1 &
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_overnight import audit_dbs
from scripts.summarize_runs import summarize_run
from src.store.db import Store
from src.web.runner import MOCK_API_TOKEN, next_run_no
from tools.replay import render_markdown

DEFAULT_TARGETS = ("julius", "tom", "robert")
_OSINT_ENV_KEYS = ("JINA_API_KEY", "EXA_API_KEY", "TAVILY_API_KEY", "APIFY_API_TOKEN")


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%SZ")


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _terminate_proc(proc: subprocess.Popen[bytes], *, grace_s: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            pass
    except OSError:
        pass


def _start_local_mock_api(token: str = MOCK_API_TOKEN) -> tuple[subprocess.Popen[bytes], str]:
    port = _pick_free_port()
    cmd = [
        sys.executable,
        "-m",
        "tests.mock_api.server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--token",
        token,
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/v1"
    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Mock API exited prematurely with code {proc.returncode}")
        try:
            resp = httpx.get(f"{url}/scenarios", headers=headers, timeout=0.5)
            if resp.status_code == 200:
                return proc, url
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    _terminate_proc(proc)
    raise RuntimeError(f"Timed out waiting for local mock API on {url}")


def _extract_stage_timings_and_errors(db_path: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Read per-stage timings and recent agent_error details from SQLite in read-only mode."""
    if not db_path.is_file():
        return {}, []

    stage_buckets: dict[str, list[tuple[int, bool]]] = defaultdict(list)
    error_samples: list[dict[str, str]] = []

    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT ts, payload_json FROM events WHERE type = 'system' ORDER BY seq ASC"
            ).fetchall()
    except sqlite3.Error:
        return {}, []

    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        kind = payload.get("kind")
        if kind == "stage_timing":
            stage = str(payload.get("stage") or "unknown")
            elapsed_ms = int(payload.get("elapsed_ms") or 0)
            ok = bool(payload.get("ok", True))
            stage_buckets[stage].append((elapsed_ms, ok))
        elif kind == "agent_error":
            error_samples.append(
                {
                    "ts": str(row["ts"]),
                    "actor": str(payload.get("failed_actor") or "unknown"),
                    "error": str(payload.get("error") or "")[:240],
                }
            )

    stage_stats: dict[str, dict[str, Any]] = {}
    for stage, samples in sorted(stage_buckets.items()):
        ms_vals = [ms for ms, _ in samples]
        failures = sum(1 for _, ok in samples if not ok)
        stage_stats[stage] = {
            "calls": len(samples),
            "failures": failures,
            "median_s": round(statistics.median(ms_vals) / 1000.0, 2),
            "max_s": round(max(ms_vals) / 1000.0, 2),
            "total_s": round(sum(ms_vals) / 1000.0, 2),
        }
    return stage_stats, error_samples


def _export_replay_markdown(db_path: Path, replay_path: Path) -> bool:
    if not db_path.is_file():
        return False
    try:
        with Store(db_path) as store:
            md = render_markdown(store)
        replay_path.parent.mkdir(parents=True, exist_ok=True)
        replay_path.write_text(md, encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


def render_batch_summary(
    *,
    batch_id: str,
    started_at: str,
    mode_label: str,
    records: list[dict[str, Any]],
    stopped_reason: str | None = None,
) -> str:
    """Build a morning triage Markdown summary across all completed runs in the batch."""
    total = len(records)
    wins = [r for r in records if r.get("state") == "WIN"]
    timeouts = [r for r in records if r.get("timed_out")]
    crashes = [r for r in records if r.get("returncode") not in (0, None) and not r.get("timed_out")]
    zero_reply = [r for r in records if r.get("outbound_turns", 0) > 0 and r.get("replies", 0) == 0]

    win_touches = [
        int(r["current_session_touches"])
        for r in wins
        if isinstance(r.get("current_session_touches"), int) and r["current_session_touches"] > 0
    ]
    median_win_touches = f"{statistics.median(win_touches):.1f}" if win_touches else "n/a"

    lines: list[str] = [
        f"# Overnight Practice Report — `{batch_id}`",
        "",
        f"- **Started:** `{started_at}`",
        f"- **Last updated:** `{_utc_iso()}`",
        f"- **Mode:** `{mode_label}`",
        f"- **Completed runs:** `{total}` | **Wins:** `{len(wins)}/{total}` | **Median touches to WIN:** `{median_win_touches}`",
        f"- **Timed out:** `{len(timeouts)}` | **Process crashes:** `{len(crashes)}` | **0-reply runs:** `{len(zero_reply)}`",
    ]
    if stopped_reason:
        lines.append(f"- **Batch status:** `{stopped_reason}`")
    lines.append("")

    # --- Per-run table ---
    lines.extend(
        [
            "## Run Results",
            "",
            "| # | Run ID | Target | Variant | State | Exit | Touches | Replies | QA (fail/tot) | Incidents | Wall (s) | Artifacts |",
            "|---:|---|---|---|---|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for idx, r in enumerate(records, start=1):
        qa_str = f"{r.get('qa_failures', 0)}/{r.get('qa_checks', 0)}"
        exit_str = "timeout" if r.get("timed_out") else str(r.get("exit_reason") or f"rc={r.get('returncode')}")
        variant = str(r.get("variant") or "standard")
        artifacts: list[str] = []
        if r.get("replay_rel"):
            artifacts.append(f"[replay]({r['replay_rel']})")
        if r.get("log_rel"):
            artifacts.append(f"[log]({r['log_rel']})")
        lines.append(
            f"| {idx} | `{r['run_id']}` | `{r['target_id']}` | `{variant}` | **{r.get('state') or 'UNKNOWN'}** "
            f"| `{exit_str}` | {r.get('current_session_touches', 0)} | {r.get('replies', 0)} "
            f"| {qa_str} | {r.get('incident_records', 0)} | {r.get('wall_s', 0.0):.1f} "
            f"| {' · '.join(artifacts) or '—'} |"
        )
    lines.append("")

    # --- Per-stage latency aggregation ---
    agg_stage_ms: dict[str, list[float]] = defaultdict(list)
    agg_stage_calls: Counter[str] = Counter()
    agg_stage_fails: Counter[str] = Counter()
    for r in records:
        for stage, st in (r.get("stage_timings") or {}).items():
            agg_stage_calls[stage] += int(st.get("calls", 0))
            agg_stage_fails[stage] += int(st.get("failures", 0))
            agg_stage_ms[stage].append(float(st.get("median_s", 0.0)))

    if agg_stage_calls:
        lines.extend(
            [
                "## Stage Latency & Reliability",
                "",
                "| Stage | Calls | Failures | Median (s) | Max Run-Median (s) |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for stage in sorted(agg_stage_calls):
            vals = agg_stage_ms[stage]
            med = statistics.median(vals) if vals else 0.0
            mx = max(vals) if vals else 0.0
            lines.append(
                f"| `{stage}` | {agg_stage_calls[stage]} | {agg_stage_fails[stage]} | {med:.2f} | {mx:.2f} |"
            )
        lines.append("")

    # --- Error breakdown & morning triage checklist ---
    err_cats: Counter[str] = Counter()
    all_error_samples: list[tuple[str, dict[str, str]]] = []
    total_qa_fails = 0
    for r in records:
        total_qa_fails += int(r.get("qa_failures", 0))
        for cat, cnt in (r.get("error_categories") or {}).items():
            err_cats[cat] += int(cnt)
        for sample in (r.get("error_samples") or [])[:5]:
            all_error_samples.append((r["run_id"], sample))

    lines.append("## Morning Triage Checklist")
    lines.append("")
    triage_items: list[str] = []
    if timeouts:
        triage_items.append(
            f"- **Wall-clock timeouts ({len(timeouts)} runs):** Inspect logs for "
            + ", ".join(f"`{r['run_id']}`" for r in timeouts)
            + " to see which stage stalled."
        )
    if crashes:
        triage_items.append(
            f"- **Non-zero exits ({len(crashes)} runs):** Check tracebacks in "
            + ", ".join(f"`{r['run_id']}` (rc={r['returncode']})" for r in crashes)
            + "."
        )
    if total_qa_fails > 0:
        triage_items.append(
            f"- **QA failures ({total_qa_fails} total):** Check rendered PNGs under `data/renders/` and QA issues in run replays."
        )
    if err_cats:
        summary_cats = ", ".join(f"`{k}`×{v}" for k, v in err_cats.most_common())
        triage_items.append(f"- **Recorded agent/API errors:** {summary_cats}.")
    if zero_reply:
        triage_items.append(
            f"- **Zero-reply runs ({len(zero_reply)}):** Review opener channel/hook fit in "
            + ", ".join(f"`{r['run_id']}`" for r in zero_reply)
            + "."
        )
    if not triage_items:
        triage_items.append("- No timeouts, crashes, QA failures, or agent errors detected.")
    lines.extend(triage_items)
    lines.append("")

    if all_error_samples:
        lines.append("## Recent Error Samples")
        lines.append("")
        for run_id, sample in all_error_samples[:20]:
            lines.append(
                f"- `{run_id}` [`{sample['actor']}`]: `{sample['error']}`"
            )
        lines.append("")

    return "\n".join(lines)


def _build_main_cmd(
    *,
    target_id: str,
    run_no: int,
    data_dir: Path,
    max_cycles: int,
    zero_pacing: bool,
    aggressive: bool,
    dry_run_stub: bool,
    stub_objective: str,
    mock_url: str | None,
    mock_token: str,
) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "src.main",
        "--scenario",
        "practice",
        "--target",
        target_id,
        "--run-no",
        str(run_no),
        "--data-dir",
        str(data_dir),
        "--max-cycles",
        str(max_cycles),
    ]
    if zero_pacing:
        cmd.append("--zero-pacing")
    if aggressive:
        cmd.append("--aggressive")
    if mock_url:
        cmd.extend(["--mock-url", mock_url, "--mock-token", mock_token])
    if dry_run_stub:
        cmd.extend(["--dry-run-stub-agents", "--stub-objective", stub_objective])
    return cmd


def run_overnight(args: argparse.Namespace) -> Path:
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    if not targets:
        raise ValueError("At least one target must be specified via --targets")

    batch_id = f"batch_{_utc_stamp()}"
    batch_dir = data_dir / "overnight" / batch_id
    logs_dir = batch_dir / "logs"
    replays_dir = batch_dir / "replays"
    logs_dir.mkdir(parents=True, exist_ok=True)
    replays_dir.mkdir(parents=True, exist_ok=True)

    results_jsonl = batch_dir / "results.jsonl"
    summary_path = batch_dir / "SUMMARY.md"
    latest_summary = data_dir / "overnight" / "LATEST_SUMMARY.md"
    latest_audit = data_dir / "overnight" / "LATEST_AUDIT.md"

    # Build round schedule: (round_label, thin_osint, aggressive)
    round_schedule: list[tuple[str, bool, bool]] = []
    for round_idx in range(1, args.rounds + 1):
        round_schedule.append((f"r{round_idx}_standard", bool(args.thin_osint), bool(args.aggressive)))
    if args.ab_aggressive and not args.aggressive:
        round_schedule.append(("ab_aggressive", bool(args.thin_osint), True))
    if args.include_thin_osint and not args.thin_osint:
        round_schedule.append(("eval_thin_osint", True, False))

    started_at = _utc_iso()
    batch_start_mono = time.monotonic()
    deadline_mono = (
        batch_start_mono + float(args.max_hours) * 3600.0
        if args.max_hours and args.max_hours > 0
        else None
    )

    stop_requested = False
    active_proc: subprocess.Popen[bytes] | None = None

    def _handle_signal(signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True
        print(f"\n[overnight] Received signal {signum}; stopping cleanly...", flush=True)
        if active_proc is not None:
            _terminate_proc(active_proc)

    prev_sigint = signal.getsignal(signal.SIGINT)
    prev_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    mock_proc: subprocess.Popen[bytes] | None = None
    mock_url: str | None = args.mock_url
    mode_label = "dry_run_stub" if args.dry_run_stub else ("mock_live_agents" if mock_url else "practice_live")

    records: list[dict[str, Any]] = []
    stopped_reason: str | None = None

    try:
        if args.dry_run_stub and not mock_url:
            mock_proc, mock_url = _start_local_mock_api(token=args.mock_token)
            print(f"[overnight] Started local mock Target API at {mock_url}", flush=True)

        print(
            f"[overnight] Starting {batch_id} | mode={mode_label} | targets={targets} | "
            f"schedule={[lbl for lbl, _, _ in round_schedule]} | max_hours={args.max_hours} | "
            f"run_timeout_s={args.run_timeout_s}",
            flush=True,
        )

        total_rounds = len(round_schedule)
        for round_idx, (round_label, use_thin_osint, use_aggressive) in enumerate(round_schedule, start=1):
            if stop_requested:
                stopped_reason = "interrupted_by_signal"
                break
            variant_name = (
                "thin_osint"
                if use_thin_osint and not use_aggressive
                else ("thin_osint+aggressive" if use_thin_osint else ("aggressive" if use_aggressive else "standard"))
            )
            for target_id in targets:
                if stop_requested:
                    stopped_reason = "interrupted_by_signal"
                    break
                if deadline_mono is not None and time.monotonic() >= deadline_mono:
                    stopped_reason = f"max_hours_reached ({args.max_hours}h)"
                    break

                run_no = next_run_no(data_dir, scenario="practice", target_id=target_id)
                run_id = f"practice_{target_id}_run{run_no}"
                db_path = data_dir / f"{run_id}.db"
                log_path = logs_dir / f"{run_id}.log"
                replay_path = replays_dir / f"{run_id}.md"

                cmd = _build_main_cmd(
                    target_id=target_id,
                    run_no=run_no,
                    data_dir=data_dir,
                    max_cycles=args.max_cycles,
                    zero_pacing=not args.use_config_pacing,
                    aggressive=use_aggressive,
                    dry_run_stub=args.dry_run_stub,
                    stub_objective=args.stub_objective,
                    mock_url=mock_url,
                    mock_token=args.mock_token,
                )

                print(
                    f"[overnight] Round {round_idx}/{total_rounds} ({round_label}) -> launching {run_id} "
                    f"[{variant_name}] (log: {log_path})",
                    flush=True,
                )
                t0 = time.monotonic()
                timed_out = False
                returncode: int | None = None

                env = dict(os.environ)
                env["PYTHONUNBUFFERED"] = "1"
                if use_thin_osint:
                    env["OSINT_CACHE"] = "0"
                    for k in _OSINT_ENV_KEYS:
                        env[k] = ""

                with log_path.open("wb") as log_fh:
                    active_proc = subprocess.Popen(
                        cmd,
                        cwd=str(PROJECT_ROOT),
                        stdout=log_fh,
                        stderr=subprocess.STDOUT,
                        env=env,
                    )
                    try:
                        returncode = active_proc.wait(timeout=float(args.run_timeout_s))
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        _terminate_proc(active_proc)
                        returncode = active_proc.returncode
                    finally:
                        active_proc = None

                wall_s = round(time.monotonic() - t0, 2)

                db_summary: dict[str, Any] = {}
                if db_path.is_file():
                    try:
                        db_summary = summarize_run(db_path)
                    except Exception as exc:  # noqa: BLE001
                        db_summary = {"summary_error": f"{type(exc).__name__}: {exc}"}

                stage_timings, error_samples = _extract_stage_timings_and_errors(db_path)
                exported_replay = _export_replay_markdown(db_path, replay_path)

                record: dict[str, Any] = {
                    "batch_id": batch_id,
                    "round": round_idx,
                    "round_label": round_label,
                    "variant": variant_name,
                    "run_id": run_id,
                    "target_id": target_id,
                    "run_no": run_no,
                    "db_path": str(db_path),
                    "log_rel": f"logs/{log_path.name}",
                    "replay_rel": f"replays/{replay_path.name}" if exported_replay else None,
                    "finished_at": _utc_iso(),
                    "wall_s": wall_s,
                    "returncode": returncode,
                    "timed_out": timed_out,
                    "state": db_summary.get("last_recorded_state"),
                    "exit_reason": db_summary.get("exit_reason"),
                    "current_session_touches": db_summary.get("current_session_touches", 0),
                    "outbound_turns": db_summary.get("outbound_turns", 0),
                    "replies": db_summary.get("replies", 0),
                    "qa_checks": db_summary.get("qa_checks", 0),
                    "qa_failures": db_summary.get("qa_failures", 0),
                    "incident_records": db_summary.get("incident_records", 0),
                    "error_categories": db_summary.get("error_categories", {}),
                    "stage_timings": stage_timings,
                    "error_samples": error_samples,
                }
                records.append(record)

                with results_jsonl.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

                summary_md = render_batch_summary(
                    batch_id=batch_id,
                    started_at=started_at,
                    mode_label=mode_label,
                    records=records,
                    stopped_reason=stopped_reason,
                )
                summary_path.write_text(summary_md, encoding="utf-8")
                latest_summary.write_text(summary_md, encoding="utf-8")

                batch_dbs = [Path(r["db_path"]) for r in records if Path(r["db_path"]).is_file()]
                if batch_dbs:
                    try:
                        audit_md_path, _ = audit_dbs(
                            batch_dbs,
                            out_dir=batch_dir,
                            renders_dir=data_dir / "renders",
                        )
                        latest_audit.write_text(audit_md_path.read_text(encoding="utf-8"), encoding="utf-8")
                    except Exception as exc:  # noqa: BLE001
                        print(f"[overnight] Audit generation warning: {exc}", flush=True)

                print(
                    f"[overnight] Finished {run_id} [{variant_name}]: state={record['state']} "
                    f"touches={record['current_session_touches']} replies={record['replies']} "
                    f"incidents={record['incident_records']} qa_fail={record['qa_failures']}/{record['qa_checks']} "
                    f"wall={wall_s:.1f}s rc={returncode}"
                    + (" [TIMEOUT]" if timed_out else ""),
                    flush=True,
                )

                if (
                    args.cooldown_s > 0
                    and not stop_requested
                    and (deadline_mono is None or time.monotonic() + args.cooldown_s < deadline_mono)
                ):
                    time.sleep(float(args.cooldown_s))

            if stopped_reason is not None:
                break

        if stopped_reason is None:
            stopped_reason = "completed_all_rounds"

        final_md = render_batch_summary(
            batch_id=batch_id,
            started_at=started_at,
            mode_label=mode_label,
            records=records,
            stopped_reason=stopped_reason,
        )
        summary_path.write_text(final_md, encoding="utf-8")
        latest_summary.write_text(final_md, encoding="utf-8")
        print(f"[overnight] Batch done ({stopped_reason}). Summary: {summary_path}", flush=True)
        return summary_path
    finally:
        if active_proc is not None:
            _terminate_proc(active_proc)
        if mock_proc is not None:
            _terminate_proc(mock_proc)
        signal.signal(signal.SIGINT, prev_sigint)
        signal.signal(signal.SIGTERM, prev_sigterm)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run practice campaigns sequentially overnight and build a morning triage summary."
    )
    parser.add_argument(
        "--targets",
        default=",".join(DEFAULT_TARGETS),
        help="Comma-separated practice target IDs (default: julius,tom,robert)",
    )
    parser.add_argument("--rounds", type=int, default=3, help="Number of rounds across all targets (default: 3)")
    parser.add_argument(
        "--max-hours",
        type=float,
        default=6.0,
        help="Stop starting new runs after this many hours (0 disables; default: 6.0)",
    )
    parser.add_argument(
        "--run-timeout-s",
        type=int,
        default=900,
        help="Hard wall-clock timeout per run in seconds (default: 900 = 15 min)",
    )
    parser.add_argument(
        "--cooldown-s",
        type=int,
        default=20,
        help="Pause between runs in seconds to stay clear of API rate limits (default: 20)",
    )
    parser.add_argument("--max-cycles", type=int, default=12, help="Max orchestrator cycles per run (default: 12)")
    parser.add_argument("--data-dir", default="data", help="Directory for SQLite DBs & overnight reports (default: data)")
    parser.add_argument(
        "--use-config-pacing",
        action="store_true",
        help="Use config.yaml touch pacing instead of --zero-pacing",
    )
    parser.add_argument("--aggressive", action="store_true", help="Pass --aggressive to src.main for all rounds")
    parser.add_argument(
        "--ab-aggressive",
        action="store_true",
        help="Append an extra A/B round with --aggressive after standard rounds",
    )
    parser.add_argument(
        "--thin-osint",
        action="store_true",
        help="Simulate hidden eval targets (OSINT_CACHE=0 and stripped OSINT API keys) for all rounds",
    )
    parser.add_argument(
        "--include-thin-osint",
        action="store_true",
        help="Append an extra eval-resilience thin-OSINT round after standard rounds",
    )
    parser.add_argument(
        "--dry-run-stub",
        action="store_true",
        help="Run deterministic stub agents against a managed local mock API for quick verification",
    )
    parser.add_argument(
        "--stub-objective",
        choices=("credentials", "action", "disclosure"),
        default="credentials",
        help="Objective when --dry-run-stub is set (default: credentials)",
    )
    parser.add_argument("--mock-url", default=None, help="Optional custom mock Target API base URL")
    parser.add_argument("--mock-token", default=MOCK_API_TOKEN, help="Bearer token for --mock-url")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    run_overnight(args)


if __name__ == "__main__":
    main()
