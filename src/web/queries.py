from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.models import DecisionRecord, Event, Snapshot
from src.store.db import AssetRecord, RegisteredPage, Store

_ASSET_URI_RE = re.compile(r"asset://([A-Za-z0-9_.-]+)")
_DATA_IMG_RE = re.compile(r"""src=["'](data:image/[^"']+)["']""", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<(?:html|body|div|p|table|img|svg|br|h[1-6]|a)\b", re.IGNORECASE)


def _file_mtime_iso(path: Path) -> str:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = datetime.now(UTC).timestamp()
    return datetime.fromtimestamp(mtime, tz=UTC).isoformat().replace("+00:00", "Z")


def _read_snapshot_timestamps(db_path: Path) -> dict[int, str]:
    """Read raw snapshot timestamps from SQLite in read-only mode."""
    result: dict[int, str] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT version, ts FROM snapshots ORDER BY version ASC").fetchall()
            for version, ts in rows:
                result[int(version)] = str(ts)
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return result


def _resolve_version_timestamps(
    versions: list[int],
    events: list[Event],
    decisions: list[DecisionRecord],
    raw_snapshot_ts: dict[int, str],
    fallback_ts: str,
) -> dict[int, str]:
    """Map each snapshot version to its best chronological timestamp.

    Events committed right before ``_bump_and_save()`` record ``base_version == v - 1``.
    Preferring event/decision timestamps preserves realistic timelines in seeded DBs
    where ``Store.save_snapshot`` writes wall-clock ``utc_now_iso()``.
    """
    event_ts_by_target_version: dict[int, str] = {}
    for ev in events:
        target_v = ev.base_version + 1 if ev.base_version >= 0 else 0
        event_ts_by_target_version[target_v] = ev.ts
        if ev.base_version == 0 and 0 not in event_ts_by_target_version:
            event_ts_by_target_version[0] = ev.ts

    decision_ts_by_version: dict[int, str] = {}
    for dec in decisions:
        decision_ts_by_version[dec.snapshot_version] = dec.ts

    resolved: dict[int, str] = {}
    earliest_event_ts = events[0].ts if events else None
    last_ts = earliest_event_ts or fallback_ts

    for v in versions:
        ts = (
            event_ts_by_target_version.get(v)
            or decision_ts_by_version.get(v)
            or (earliest_event_ts if v <= 1 and earliest_event_ts is not None else None)
            or raw_snapshot_ts.get(v)
            or last_ts
        )
        resolved[v] = ts
        last_ts = ts
    return resolved


def _snapshot_at_or_before(
    version: int,
    versions: list[int],
    snapshots: dict[int, Snapshot],
) -> Snapshot | None:
    chosen: int | None = None
    for v in versions:
        if v <= version:
            chosen = v
        else:
            break
    if chosen is None and versions:
        chosen = versions[0]
    return snapshots.get(chosen) if chosen is not None else None


def _pick_signal_field(
    sig: dict[str, Any],
    outer: dict[str, Any],
    key: str,
    default: Any = None,
) -> Any:
    if key in sig and sig[key] is not None:
        return sig[key]
    if key in outer and outer[key] is not None:
        return outer[key]
    return default


def _extract_signal_series(
    events: list[Event],
    versions: list[int],
    snapshots: dict[int, Snapshot],
) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    last_channel = "email"

    for ev in events:
        if ev.type in {"target_event", "agent_output"} and ev.payload.get("channel"):
            last_channel = str(ev.payload["channel"])

        if ev.type != "agent_output" or ev.payload.get("kind") != "signals":
            continue

        raw_inner = ev.payload.get("payload")
        sig: dict[str, Any] = raw_inner if isinstance(raw_inner, dict) else ev.payload

        raw_touch = _pick_signal_field(sig, ev.payload, "touch_n")
        if raw_touch is not None:
            touch_n = int(raw_touch)
        else:
            snap = _snapshot_at_or_before(ev.base_version, versions, snapshots)
            touch_n = snap.campaign.budget.used if snap is not None else 0

        raw_channel = _pick_signal_field(sig, ev.payload, "channel")
        channel = str(raw_channel) if raw_channel else last_channel

        raw_hits = _pick_signal_field(sig, ev.payload, "hook_hits", [])
        hook_hits = [str(h) for h in raw_hits] if isinstance(raw_hits, list) else []

        series.append(
            {
                "seq": len(series) + 1,
                "touch_n": touch_n,
                "version": ev.base_version,
                "ts": ev.ts,
                "channel": channel,
                "engagement": round(float(_pick_signal_field(sig, ev.payload, "engagement", 0.0)), 4),
                "suspicion": round(float(_pick_signal_field(sig, ev.payload, "suspicion", 0.0)), 4),
                "mood": str(_pick_signal_field(sig, ev.payload, "mood", "neutral")),
                "hook_hits": hook_hits,
                "proposed_next": str(_pick_signal_field(sig, ev.payload, "proposed_next", "")),
                "reason": str(_pick_signal_field(sig, ev.payload, "reason", "")),
            }
        )

    return series


def _extract_portrait_asset_id(store: Store, latest: Snapshot) -> str | None:
    ident = latest.identity
    if ident is None:
        return None
    if ident.portrait.api_asset_id:
        return ident.portrait.api_asset_id
    if ident.portrait.local:
        rec = store.get_asset_by_path(ident.portrait.local)
        if rec is not None:
            return rec.asset_id
    return None


def _compute_updated_at(
    db_path: Path,
    latest: Snapshot,
    events: list[Event],
    decisions: list[DecisionRecord],
) -> str:
    candidates: list[str] = []
    if events:
        candidates.append(events[-1].ts)
    if decisions:
        candidates.append(decisions[-1].ts)
    if latest.conversation.turns:
        candidates.append(latest.conversation.turns[-1].ts)
    if latest.facts:
        candidates.extend(f.ts for f in latest.facts if f.ts)
    if candidates:
        return max(candidates)
    return _file_mtime_iso(db_path)


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def _build_execution_status(
    *,
    events: list[Event],
    latest: Snapshot,
    is_running: bool,
    fallback_ts: str,
) -> dict[str, Any]:
    """Build live & historical per-stage execution telemetry for one campaign run.

    Uses explicit ``stage_start`` / ``stage_timing`` events when present, and
    gracefully falls back to inferring completed stage steps from ``agent_output``
    event timestamps for older saved databases.
    """
    now_dt = datetime.now(UTC)
    started_at: str | None = events[0].ts if events else None
    last_event_ts: str | None = events[-1].ts if events else None
    exit_reason: str | None = None

    has_explicit_timings = any(
        ev.type == "system" and ev.payload.get("kind") in {"stage_start", "stage_timing"}
        for ev in events
    )

    open_steps: dict[str, dict[str, Any]] = {}
    open_order: list[str] = []
    completed_steps: list[dict[str, Any]] = []
    seq_counter = 0

    for ev in events:
        if ev.type == "system":
            kind = ev.payload.get("kind")
            if kind == "run_exit":
                raw_reason = ev.payload.get("reason")
                if isinstance(raw_reason, str) and raw_reason.strip():
                    exit_reason = raw_reason.strip()
            elif kind == "stage_start":
                seq_counter += 1
                stage = str(ev.payload.get("stage") or "unknown")
                mode = str(ev.payload.get("mode") or "")
                step_id = str(ev.payload.get("step_id") or f"step_{seq_counter}")
                open_steps[step_id] = {
                    "step_id": step_id,
                    "stage": stage,
                    "mode": mode,
                    "started_at": ev.ts,
                }
                open_order.append(step_id)
            elif kind == "stage_timing":
                seq_counter += 1
                stage = str(ev.payload.get("stage") or "unknown")
                mode = str(ev.payload.get("mode") or "")
                step_id = str(ev.payload.get("step_id") or "")
                elapsed_ms = max(0, int(ev.payload.get("elapsed_ms") or 0))
                ok = bool(ev.payload.get("ok", True))

                matched = open_steps.pop(step_id, None) if step_id else None
                if matched is None:
                    # Fallback match by stage+mode when step_id was omitted
                    for oid in open_order:
                        cand = open_steps.get(oid)
                        if cand is not None and cand["stage"] == stage and cand["mode"] == mode:
                            matched = open_steps.pop(oid)
                            break

                completed_steps.append(
                    {
                        "step_id": step_id or (matched["step_id"] if matched else f"step_{seq_counter}"),
                        "stage": stage,
                        "mode": mode,
                        "started_at": matched["started_at"] if matched else ev.ts,
                        "finished_at": ev.ts,
                        "elapsed_ms": elapsed_ms,
                        "ok": ok,
                        "inferred": False,
                    }
                )

    if not has_explicit_timings:
        prev_dt = _parse_iso_dt(started_at)
        for idx, ev in enumerate(events, start=1):
            cur_dt = _parse_iso_dt(ev.ts)
            if ev.type == "agent_output" and ev.actor in {
                "osint",
                "identity",
                "profile",
                "strategy",
                "messaging",
                "qa",
            }:
                delta_ms = 0
                if prev_dt is not None and cur_dt is not None:
                    delta_ms = max(0, int((cur_dt - prev_dt).total_seconds() * 1000))
                kind_str = str(ev.payload.get("kind") or ev.payload.get("direction") or "")
                completed_steps.append(
                    {
                        "step_id": f"inferred_{idx}",
                        "stage": str(ev.actor),
                        "mode": kind_str,
                        "started_at": prev_dt.isoformat().replace("+00:00", "Z") if prev_dt else ev.ts,
                        "finished_at": ev.ts,
                        "elapsed_ms": delta_ms,
                        "ok": True,
                        "inferred": True,
                    }
                )
            if cur_dt is not None:
                prev_dt = cur_dt

    # Active steps: any open stage_start when run is still active (or not yet exited)
    is_terminal = latest.run.state in {"WIN", "DONE", "ABORT"} or exit_reason is not None
    effectively_running = is_running or (bool(open_steps) and not is_terminal)
    active_steps: list[dict[str, Any]] = []
    if effectively_running:
        for oid in open_order:
            item = open_steps.get(oid)
            if item is None:
                continue
            st_dt = _parse_iso_dt(item["started_at"])
            elapsed_ms = (
                max(0, int((now_dt - st_dt).total_seconds() * 1000))
                if st_dt is not None
                else 0
            )
            active_steps.append(
                {
                    "step_id": item["step_id"],
                    "stage": item["stage"],
                    "mode": item["mode"],
                    "started_at": item["started_at"],
                    "elapsed_ms": elapsed_ms,
                }
            )

    # Per-stage rollup
    stage_agg: dict[str, dict[str, Any]] = {}
    for step in completed_steps:
        stage = step["stage"]
        bucket = stage_agg.setdefault(
            stage,
            {
                "stage": stage,
                "calls": 0,
                "failures": 0,
                "total_ms": 0,
                "max_ms": 0,
                "last_ms": 0,
            },
        )
        ms = int(step["elapsed_ms"])
        bucket["calls"] += 1
        if not step["ok"]:
            bucket["failures"] += 1
        bucket["total_ms"] += ms
        bucket["max_ms"] = max(bucket["max_ms"], ms)
        bucket["last_ms"] = ms

    stage_totals: list[dict[str, Any]] = []
    for bucket in sorted(stage_agg.values(), key=lambda x: x["total_ms"], reverse=True):
        calls = max(1, bucket["calls"])
        stage_totals.append(
            {
                **bucket,
                "avg_ms": round(bucket["total_ms"] / calls),
            }
        )

    start_dt = _parse_iso_dt(started_at)
    end_dt = None if effectively_running else _parse_iso_dt(last_event_ts or fallback_ts)
    if start_dt is not None:
        ref_end = now_dt if effectively_running else (end_dt or start_dt)
        total_elapsed_ms = max(0, int((ref_end - start_dt).total_seconds() * 1000))
    else:
        total_elapsed_ms = sum(int(s["elapsed_ms"]) for s in completed_steps)

    return {
        "is_running": effectively_running,
        "started_at": started_at,
        "finished_at": None if effectively_running else (last_event_ts or fallback_ts),
        "total_elapsed_ms": total_elapsed_ms,
        "exit_reason": exit_reason,
        "active_steps": active_steps,
        "completed_steps": completed_steps,
        "stage_totals": stage_totals,
    }


def _build_target_summary(
    *,
    db_path: Path,
    store: Store,
    latest: Snapshot,
    events: list[Event],
    decisions: list[DecisionRecord],
    signal_series: list[dict[str, Any]],
    is_running: bool,
    execution_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    latest_sig = signal_series[-1] if signal_series else None
    target_id = latest.target.id or latest.run.target_id or db_path.stem
    target_name = latest.target.name or target_id
    exec_info = execution_status or _build_execution_status(
        events=events,
        latest=latest,
        is_running=is_running,
        fallback_ts=_file_mtime_iso(db_path),
    )
    active_steps = exec_info.get("active_steps") or []
    active_stage = str(active_steps[-1]["stage"]) if active_steps else None
    active_mode = str(active_steps[-1]["mode"]) if active_steps and active_steps[-1].get("mode") else None

    return {
        "run_id": db_path.stem,
        "scenario": latest.run.scenario,
        "target_id": target_id,
        "target_name": target_name,
        "target_role": latest.target.role,
        "target_org": latest.target.org,
        "state": latest.run.state,
        "touches_used": latest.campaign.budget.used,
        "session_max": latest.campaign.budget.session_max,
        "sessions_used": latest.sessions.used,
        "sessions_allowed": latest.sessions.allowed,
        "latest_suspicion": float(latest_sig["suspicion"]) if latest_sig is not None else None,
        "latest_engagement": float(latest_sig["engagement"]) if latest_sig is not None else None,
        "latest_mood": str(latest_sig["mood"]) if latest_sig is not None else None,
        "profile_confidence": round(float(latest.profile.overall_confidence), 4),
        "incidents_count": len(latest.incidents),
        "sender_name": latest.identity.sender_name if latest.identity is not None else "",
        "sender_domain": latest.identity.domain if latest.identity is not None else "",
        "portrait_asset_id": _extract_portrait_asset_id(store, latest),
        "latest_version": latest.version,
        "updated_at": _compute_updated_at(db_path, latest, events, decisions),
        "is_running": bool(exec_info["is_running"]),
        "active_stage": active_stage,
        "active_mode": active_mode,
        "total_elapsed_ms": int(exec_info["total_elapsed_ms"]),
        "exit_reason": exec_info["exit_reason"],
    }


def _build_stage_history(
    versions: list[int],
    snapshots: dict[int, Snapshot],
    events: list[Event],
    version_ts: dict[int, str],
) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    if versions:
        first_v = versions[0]
        first_snap = snapshots[first_v]
        history.append(
            {
                "stage": first_snap.run.state,
                "from_stage": None,
                "ts": version_ts.get(first_v, ""),
                "version": first_v,
                "reason": "run initialized",
            }
        )

    for ev in events:
        if ev.type == "system" and ev.payload.get("kind") == "state_transition":
            to_stage = str(ev.payload.get("to") or "")
            raw_from = ev.payload.get("from")
            from_stage = str(raw_from) if raw_from is not None else None
            ver = ev.base_version + 1 if (ev.base_version + 1) in snapshots else ev.base_version
            history.append(
                {
                    "stage": to_stage,
                    "from_stage": from_stage,
                    "ts": ev.ts,
                    "version": ver,
                    "reason": str(ev.payload.get("reason") or ""),
                }
            )
        elif ev.type == "incident":
            ver = ev.base_version + 1 if (ev.base_version + 1) in snapshots else ev.base_version
            if history and history[-1]["stage"] == "WIN" and history[-1]["version"] == ver:
                continue
            prev_stage = str(history[-1]["stage"]) if history else "ENGAGE"
            reason = str(
                ev.payload.get("summary")
                or ev.payload.get("objective")
                or "objective incident achieved"
            )
            history.append(
                {
                    "stage": "WIN",
                    "from_stage": prev_stage,
                    "ts": ev.ts,
                    "version": ver,
                    "reason": reason,
                }
            )

    return history


def _collect_all_pages(
    store: Store,
    snapshots: dict[int, Snapshot],
    events: list[Event],
) -> list[RegisteredPage]:
    session_ids: list[str] = []
    seen_sids: set[str] = set()

    def _add_sid(sid: str | None) -> None:
        if sid and sid not in seen_sids:
            seen_sids.add(sid)
            session_ids.append(sid)

    for snap in snapshots.values():
        _add_sid(snap.sessions.current)
        _add_sid(snap.conversation.session_id)
    for ev in events:
        if ev.type == "session":
            raw_sid = ev.payload.get("session_id")
            if isinstance(raw_sid, str):
                _add_sid(raw_sid)

    pages: list[RegisteredPage] = []
    seen_keys: set[tuple[str, str]] = set()
    for sid in session_ids:
        for page in store.get_pages_for_session(sid):
            key = (page.session_id, page.url)
            if key not in seen_keys:
                seen_keys.add(key)
                pages.append(page)
    return pages


def _build_event_markers(
    versions: list[int],
    snapshots: dict[int, Snapshot],
    events: list[Event],
    decisions: list[DecisionRecord],
    pages: list[RegisteredPage],
) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    target_marker_kinds = {"link.opened", "page.submitted", "attachment.opened"}

    def _touch_for(ver: int) -> int:
        snap = _snapshot_at_or_before(ver, versions, snapshots)
        return snap.campaign.budget.used if snap is not None else 0

    new_session_versions: set[int] = set()
    pivot_versions: list[int] = []
    has_register_page_marker = False

    for ev in events:
        ver = ev.base_version + 1 if (ev.base_version + 1) in snapshots else ev.base_version
        if ev.type == "target_event":
            ev_kind = str(ev.payload.get("event") or "")
            if ev_kind in target_marker_kinds:
                raw_data = ev.payload.get("data")
                data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
                detail = str(
                    data.get("url")
                    or data.get("filename")
                    or data.get("page_id")
                    or ev.payload.get("body")
                    or ""
                ).strip()
                label = f"{ev_kind}: {detail}" if detail else ev_kind
                markers.append(
                    {
                        "version": ver,
                        "touch_n": _touch_for(ver),
                        "ts": ev.ts,
                        "kind": ev_kind,
                        "label": label,
                    }
                )
        elif ev.type == "incident":
            obj = str(ev.payload.get("objective") or "win")
            summary = str(ev.payload.get("summary") or "Objective achieved")
            markers.append(
                {
                    "version": ver,
                    "touch_n": _touch_for(ver),
                    "ts": ev.ts,
                    "kind": "incident",
                    "label": f"WIN ({obj}): {summary}",
                }
            )
        elif ev.type == "session":
            sessions_used = int(ev.payload.get("sessions_used") or 1)
            if sessions_used > 1:
                sid = str(ev.payload.get("session_id") or "")
                new_session_versions.add(ver)
                markers.append(
                    {
                        "version": ver,
                        "touch_n": _touch_for(ver),
                        "ts": ev.ts,
                        "kind": "new_session",
                        "label": f"Session #{sessions_used} started ({sid})"
                        if sid
                        else f"Session #{sessions_used} started",
                    }
                )
        elif ev.type == "agent_output" and ev.actor == "strategy" and ev.payload.get("kind") == "plan":
            rationale = str(ev.payload.get("rationale") or "")
            raw_inner = ev.payload.get("payload")
            inner: dict[str, Any] = raw_inner if isinstance(raw_inner, dict) else {}
            mode = str(inner.get("mode") or ev.payload.get("mode") or "")
            if "pivot" in rationale.lower() or mode.lower() == "pivot":
                pivot_versions.append(ver)
                markers.append(
                    {
                        "version": ver,
                        "touch_n": _touch_for(ver),
                        "ts": ev.ts,
                        "kind": "pivot",
                        "label": f"Strategy pivot: {rationale}" if rationale else "Strategy pivot",
                    }
                )

    for dec in decisions:
        ver = dec.snapshot_version
        for action in dec.actions:
            extra = action.model_extra or {}
            if action.op == "register_page":
                has_register_page_marker = True
                title = str(extra.get("title") or extra.get("url") or "Landing page")
                markers.append(
                    {
                        "version": ver,
                        "touch_n": _touch_for(ver),
                        "ts": dec.ts,
                        "kind": "register_page",
                        "label": f"Registered page: {title}",
                    }
                )
            elif action.op == "new_session":
                if not any(abs(ver - existing_v) <= 2 for existing_v in new_session_versions):
                    reason = str(extra.get("reason") or "Session restarted")
                    new_session_versions.add(ver)
                    markers.append(
                        {
                            "version": ver,
                            "touch_n": _touch_for(ver),
                            "ts": dec.ts,
                            "kind": "new_session",
                            "label": f"New session: {reason}",
                        }
                    )
            elif (
                action.op == "trigger"
                and str(extra.get("agent") or "") == "strategy"
                and not any(abs(ver - existing_v) <= 2 for existing_v in pivot_versions)
            ):
                reason = str(extra.get("reason") or "Strategy pivot")
                pivot_versions.append(ver)
                markers.append(
                    {
                        "version": ver,
                        "touch_n": _touch_for(ver),
                        "ts": dec.ts,
                        "kind": "pivot",
                        "label": f"Pivot: {reason}",
                    }
                )

    if pages and not has_register_page_marker:
        latest_v = versions[-1] if versions else 0
        for page in pages:
            markers.append(
                {
                    "version": latest_v,
                    "touch_n": _touch_for(latest_v),
                    "ts": page.created_at,
                    "kind": "register_page",
                    "label": f"Registered page: {page.title or page.url}",
                }
            )

    markers.sort(key=lambda m: (int(m["version"]), str(m["ts"])))
    return markers


def _build_personality_evolution(
    versions: list[int],
    snapshots: dict[int, Snapshot],
    version_ts: dict[int, str],
    signal_series: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not versions:
        return [], []

    # Watchout history scans every version in ascending order so first_seen_version is exact.
    watchout_history: list[dict[str, Any]] = []
    seen_watchouts_all: set[str] = set()
    for v in versions:
        snap = snapshots[v]
        for text in snap.profile.watchouts:
            if text not in seen_watchouts_all:
                seen_watchouts_all.add(text)
                watchout_history.append(
                    {
                        "text": text,
                        "first_seen_version": v,
                        "first_seen_touch": snap.campaign.budget.used,
                        "ts": version_ts.get(v, ""),
                    }
                )

    # Select meaningful milestone versions.
    latest_v = versions[-1]
    start_v = 1 if 1 in snapshots else versions[0]
    selected_versions: list[int] = []
    last_fingerprint: tuple[Any, ...] | None = None
    saw_first_profile = False

    for v in versions:
        if v < start_v and v != latest_v:
            continue
        snap = snapshots[v]
        profile_dump = snap.profile.model_dump(mode="json")
        hooks_dump = [h.model_dump(mode="json") for h in snap.hooks]
        has_profile = bool(
            snap.profile.psych
            or snap.profile.style_guide
            or snap.profile.trust_style
            or snap.profile.watchouts
            or snap.profile.overall_confidence > 0.0
        )
        fingerprint = (
            profile_dump,
            hooks_dump,
            snap.run.state,
            snap.campaign.budget.used,
            snap.sessions.used,
        )

        should_emit = (
            v in {start_v, latest_v}
            or (has_profile and not saw_first_profile)
            or fingerprint != last_fingerprint
        )

        if should_emit:
            selected_versions.append(v)
            last_fingerprint = fingerprint
            if has_profile:
                saw_first_profile = True

    # Build milestone dicts with trait deltas vs earliest milestone that had any psych traits.
    milestones: list[dict[str, Any]] = []
    baseline_traits: dict[str, float] | None = None
    seen_milestone_watchouts: set[str] = set()
    prev_state: str | None = None
    prev_touch: int = -1
    prev_sessions: int = 0
    saw_profile_milestone = False

    for v in selected_versions:
        snap = snapshots[v]
        profile_dump = snap.profile.model_dump(mode="json")
        hooks_dump = [h.model_dump(mode="json") for h in snap.hooks]

        watchouts_added = [
            w for w in snap.profile.watchouts if w not in seen_milestone_watchouts
        ]
        seen_milestone_watchouts.update(watchouts_added)

        traits_out: list[dict[str, Any]] = []
        if snap.profile.psych:
            if baseline_traits is None:
                baseline_traits = {t.trait: float(t.confidence) for t in snap.profile.psych}
                for t in snap.profile.psych:
                    conf = round(float(t.confidence), 4)
                    traits_out.append(
                        {
                            "trait": t.trait,
                            "initial_confidence": conf,
                            "confidence": conf,
                            "delta": 0.0,
                            "is_new": False,
                            "evidence": list(t.evidence),
                        }
                    )
            else:
                for t in snap.profile.psych:
                    conf = round(float(t.confidence), 4)
                    if t.trait in baseline_traits:
                        init_c = round(baseline_traits[t.trait], 4)
                        traits_out.append(
                            {
                                "trait": t.trait,
                                "initial_confidence": init_c,
                                "confidence": conf,
                                "delta": round(conf - init_c, 4),
                                "is_new": False,
                                "evidence": list(t.evidence),
                            }
                        )
                    else:
                        traits_out.append(
                            {
                                "trait": t.trait,
                                "initial_confidence": 0.0,
                                "confidence": conf,
                                "delta": conf,
                                "is_new": True,
                                "evidence": list(t.evidence),
                            }
                        )

        # Latest signal at or before this snapshot version.
        latest_sig: dict[str, Any] | None = None
        for sig in signal_series:
            if int(sig["version"]) <= v:
                latest_sig = sig
            else:
                break

        has_profile_now = bool(snap.profile.psych or snap.profile.overall_confidence > 0.0)
        if snap.run.state == "WIN" and prev_state != "WIN":
            label = "WIN — Incident Achieved"
        elif snap.sessions.used > prev_sessions and snap.sessions.used > 1:
            label = f"Session {snap.sessions.used} Restart"
        elif prev_state is None:
            label = f"Initial ({snap.run.state})"
        elif snap.run.state != prev_state:
            label = f"Stage: {snap.run.state}"
        elif has_profile_now and not saw_profile_milestone:
            label = "Initial Profile Synthesis"
        elif snap.campaign.budget.used != prev_touch and snap.campaign.budget.used > 0:
            label = f"Touch {snap.campaign.budget.used}"
        elif watchouts_added:
            label = f"Profile Update (+{len(watchouts_added)} watchout)"
        else:
            label = f"Profile Refined (v{v})"

        if has_profile_now:
            saw_profile_milestone = True
        prev_state = snap.run.state
        prev_touch = snap.campaign.budget.used
        prev_sessions = snap.sessions.used

        milestones.append(
            {
                "version": v,
                "ts": version_ts.get(v, ""),
                "touch_n": snap.campaign.budget.used,
                "label": label,
                "state": snap.run.state,
                "profile": profile_dump,
                "traits": traits_out,
                "hooks": hooks_dump,
                "watchouts_added": watchouts_added,
                "suspicion": float(latest_sig["suspicion"]) if latest_sig is not None else None,
                "engagement": float(latest_sig["engagement"]) if latest_sig is not None else None,
                "mood": str(latest_sig["mood"]) if latest_sig is not None else None,
            }
        )

    return milestones, watchout_history


def list_targets(
    data_dir: Path,
    running_run_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Scan all SQLite WAL databases in ``data_dir`` and return target summaries."""
    active_ids = running_run_ids or set()
    targets: list[dict[str, Any]] = []

    if data_dir.exists():
        for db_path in sorted(data_dir.glob("*.db")):
            try:
                with Store(db_path) as store:
                    latest = store.latest_snapshot()
                    if latest is None:
                        continue
                    versions = store.list_snapshot_versions()
                    snapshots: dict[int, Snapshot] = {}
                    for v in versions:
                        snap = store.get_snapshot(v)
                        if snap is not None:
                            snapshots[v] = snap
                    events = store.get_events()
                    decisions = store.get_decisions()
                    signal_series = _extract_signal_series(events, versions, snapshots)
                    summary = _build_target_summary(
                        db_path=db_path,
                        store=store,
                        latest=latest,
                        events=events,
                        decisions=decisions,
                        signal_series=signal_series,
                        is_running=db_path.stem in active_ids,
                    )
                    targets.append(summary)
            except (sqlite3.Error, OSError, ValueError):
                continue

    targets.sort(key=lambda item: (str(item["updated_at"]), str(item["run_id"])), reverse=True)
    return {
        "data_dir": str(data_dir),
        "targets": targets,
    }


def get_target_detail(
    db_path: Path,
    is_running: bool = False,
) -> dict[str, Any]:
    """Read full campaign history, signals, personality evolution, and artifacts for one DB."""
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")

    raw_snapshot_ts = _read_snapshot_timestamps(db_path)
    with Store(db_path) as store:
        latest = store.latest_snapshot()
        if latest is None:
            raise ValueError(f"Database has no snapshots: {db_path}")

        versions = store.list_snapshot_versions()
        snapshots: dict[int, Snapshot] = {}
        for v in versions:
            snap = store.get_snapshot(v)
            if snap is not None:
                snapshots[v] = snap

        events = store.get_events()
        decisions = store.get_decisions()
        pages = _collect_all_pages(store, snapshots, events)

        version_ts = _resolve_version_timestamps(
            versions,
            events,
            decisions,
            raw_snapshot_ts,
            fallback_ts=_file_mtime_iso(db_path),
        )
        signal_series = _extract_signal_series(events, versions, snapshots)
        execution_status = _build_execution_status(
            events=events,
            latest=latest,
            is_running=is_running,
            fallback_ts=_file_mtime_iso(db_path),
        )
        summary = _build_target_summary(
            db_path=db_path,
            store=store,
            latest=latest,
            events=events,
            decisions=decisions,
            signal_series=signal_series,
            is_running=is_running,
            execution_status=execution_status,
        )
        stage_history = _build_stage_history(versions, snapshots, events, version_ts)
        event_markers = _build_event_markers(versions, snapshots, events, decisions, pages)
        personality_evolution, watchout_history = _build_personality_evolution(
            versions,
            snapshots,
            version_ts,
            signal_series,
        )

        assets_by_id = _build_assets_map(db_path.stem, store.list_assets())

        events_by_id = {ev.event_id: ev for ev in events}
        turns: list[dict[str, Any]] = []
        for turn in latest.conversation.turns:
            turn_dict = turn.model_dump(mode="json")
            turn_dict["event_ref"] = turn.event_ref
            ref_ev = events_by_id.get(turn.event_ref)
            raw_attachments: list[Any] = []
            if ref_ev is not None:
                if ref_ev.payload.get("subject") is not None:
                    turn_dict["subject"] = ref_ev.payload["subject"]
                if isinstance(ref_ev.payload.get("facts_used"), list):
                    turn_dict["facts_used"] = list(ref_ev.payload["facts_used"])
                if isinstance(ref_ev.payload.get("attachments"), list):
                    raw_attachments = list(ref_ev.payload["attachments"])

            turn_dict["is_html"] = bool(_HTML_TAG_RE.search(turn.body))
            turn_dict["inline_images"] = _extract_inline_images(turn.body, assets_by_id)
            turn_dict["attachments"] = _normalize_attachments(raw_attachments, assets_by_id)
            turns.append(turn_dict)

        registered_pages = [
            {
                "session_id": p.session_id,
                "page_id": p.page_id,
                "url": p.url,
                "title": p.title,
                "fields": list(p.fields),
                "created_at": p.created_at,
            }
            for p in pages
        ]

        facts_by_id = {fact.fact_id: fact.model_dump(mode="json") for fact in latest.facts}

        return {
            "run_id": db_path.stem,
            "summary": summary,
            "execution_status": execution_status,
            "latest_snapshot": latest.model_dump(mode="json"),
            "stage_history": stage_history,
            "signal_series": signal_series,
            "event_markers": event_markers,
            "personality_evolution": personality_evolution,
            "watchout_history": watchout_history,
            "turns": turns,
            "events": [ev.model_dump(mode="json") for ev in events],
            "decisions": [dec.model_dump(mode="json") for dec in decisions],
            "registered_pages": registered_pages,
            "facts_by_id": facts_by_id,
            "assets_by_id": assets_by_id,
        }


def _build_assets_map(run_id: str, assets: list[AssetRecord]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for asset in assets:
        local = Path(asset.local_path)
        out[asset.asset_id] = {
            "asset_id": asset.asset_id,
            "filename": local.name or asset.asset_id,
            "content_type": asset.content_type,
            "size_bytes": asset.size_bytes,
            "url": f"/api/targets/{run_id}/assets/{asset.asset_id}",
            "is_image": asset.content_type.startswith("image/"),
            "exists": local.is_file(),
        }
    return out


def _extract_inline_images(
    body: str,
    assets_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _ASSET_URI_RE.finditer(body):
        aid = match.group(1)
        if aid in seen:
            continue
        seen.add(aid)
        meta = assets_by_id.get(aid)
        if meta is not None:
            images.append(dict(meta))
        else:
            images.append(
                {
                    "asset_id": aid,
                    "filename": aid,
                    "content_type": "image/png",
                    "size_bytes": 0,
                    "url": "",
                    "is_image": True,
                    "exists": False,
                }
            )
    for idx, match in enumerate(_DATA_IMG_RE.finditer(body), start=1):
        uri = match.group(1)
        if uri in seen:
            continue
        seen.add(uri)
        images.append(
            {
                "asset_id": f"inline_{idx}",
                "filename": f"inline_image_{idx}",
                "content_type": "image/png",
                "size_bytes": len(uri),
                "url": uri,
                "is_image": True,
                "exists": True,
            }
        )
    return images


def _normalize_attachments(
    raw_attachments: list[Any],
    assets_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in raw_attachments:
        if isinstance(item, str):
            aid = item.removeprefix("asset://")
            meta = assets_by_id.get(aid)
            if meta is not None:
                normalized.append(
                    {
                        "kind": "image" if meta["is_image"] else "file",
                        "asset_id": aid,
                        "url": meta["url"],
                        "filename": meta["filename"],
                        "content_type": meta["content_type"],
                        "exists": meta["exists"],
                        "transcript": None,
                    }
                )
            else:
                normalized.append(
                    {
                        "kind": "file",
                        "asset_id": aid,
                        "url": None,
                        "filename": aid,
                        "content_type": None,
                        "exists": False,
                        "transcript": None,
                    }
                )
        elif isinstance(item, dict):
            filename = str(item.get("filename") or item.get("name") or "attachment")
            transcript_raw = item.get("transcript")
            transcript = str(transcript_raw) if transcript_raw is not None else None
            normalized.append(
                {
                    "kind": "virtual",
                    "asset_id": None,
                    "url": None,
                    "filename": filename,
                    "content_type": None,
                    "exists": True,
                    "transcript": transcript,
                }
            )
    return normalized


def get_snapshot_at_version(db_path: Path, version: int) -> dict[str, Any] | None:
    """Return the serialized Snapshot at an exact version, or None if not found."""
    if not db_path.is_file():
        return None
    with Store(db_path) as store:
        snap = store.get_snapshot(version)
        if snap is None:
            return None
        return snap.model_dump(mode="json")
