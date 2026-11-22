from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from src.config import load_config
from src.models import utc_now_iso
from src.web.demo_seed import seed_demo_data
from src.web.offline_replay import write_acknowledgment_replay
from src.web.queries import get_snapshot_at_version, get_target_detail, list_targets
from src.web.runner import RunManager

RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class LaunchRunRequest(BaseModel):
    target_id: str
    mode: Literal["mock_stub", "practice_live"] = "practice_live"
    stub_objective: Literal["credentials", "action", "disclosure"] = "credentials"
    zero_pacing: bool = True
    aggressive: bool = True
    scenario: str = "practice"


class LaunchEvalRequest(BaseModel):
    target_ids: list[str] = Field(default_factory=list)
    run_all: bool = False
    zero_pacing: bool = True
    aggressive: bool = True
    confirm: bool = False


class OfflineReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture: Literal["acknowledgment"]


async def _fetch_eval_scenario_metadata() -> dict[str, Any]:
    """Read-only GET /v1/scenarios lookup for the eval scenario (0 sessions/touches spent)."""
    cfg = load_config("config.yaml")
    base_url = cfg.target_api.base_url.rstrip("/")
    team_key = cfg.target_api.team_key
    if not base_url or not team_key:
        raise HTTPException(
            status_code=500,
            detail="TARGET_API_URL or TEAM_API_KEY is not configured.",
        )
    url = f"{base_url}/scenarios"
    headers = {"Authorization": f"Bearer {team_key}"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to reach Target API /v1/scenarios: {exc}",
        ) from exc

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502 if resp.status_code >= 500 else resp.status_code,
            detail=f"Target API /v1/scenarios returned HTTP {resp.status_code}: {resp.text[:240]}",
        )

    raw = resp.json()
    scenarios = raw.get("scenarios", []) if isinstance(raw, dict) else raw
    if not isinstance(scenarios, list):
        raise HTTPException(status_code=502, detail="Unexpected /v1/scenarios response shape.")

    eval_sc = next(
        (
            sc
            for sc in scenarios
            if isinstance(sc, dict) and (sc.get("id") == "eval" or sc.get("kind") == "eval")
        ),
        None,
    )
    if eval_sc is None:
        raise HTTPException(
            status_code=404,
            detail="No 'eval' scenario returned by Target API /v1/scenarios.",
        )

    targets = [
        {
            "id": str(t.get("id") or ""),
            "name": str(t.get("name") or t.get("id") or ""),
            "role": str(t.get("role") or ""),
        }
        for t in eval_sc.get("targets", [])
        if isinstance(t, dict) and t.get("id")
    ]
    objectives = [
        {
            "id": str(o.get("id") or ""),
            "text": str(o.get("text") or ""),
        }
        for o in eval_sc.get("objectives", [])
        if isinstance(o, dict)
    ]
    return {
        "id": str(eval_sc.get("id") or "eval"),
        "open": bool(eval_sc.get("open", True)),
        "company": str(eval_sc.get("company") or ""),
        "touch_budget": int(eval_sc.get("touch_budget") or 10),
        "max_sessions": int(eval_sc.get("max_sessions") or 15),
        "targets": targets,
        "objectives": objectives,
    }


def _resolve_db_path(data_dir: Path, run_id: str) -> Path:
    if not RUN_ID_RE.match(run_id):
        raise HTTPException(status_code=400, detail=f"Invalid run_id: {run_id!r}")
    base_dir = data_dir.resolve()
    candidate = (base_dir / f"{run_id}.db").resolve()
    if candidate.parent != base_dir:
        raise HTTPException(status_code=400, detail="Invalid run_id path")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return candidate


def _read_asset_readonly(db_path: Path, asset_id: str) -> tuple[str, str] | None:
    """Look up (local_path, content_type) for an asset using a read-only SQLite connection."""
    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            row = conn.execute(
                "SELECT local_path, content_type FROM assets WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            if row is None:
                return None
            return str(row[0]), str(row[1])
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _compute_stream_fingerprint(
    data_dir: Path,
    run_manager: RunManager,
) -> tuple[Any, ...]:
    files_state: list[tuple[str, int, int]] = []
    if data_dir.exists():
        # Watch only .db and .db-wal (never .db-shm, which SQLite touches on reads)
        for pattern in ("*.db", "*.db-wal"):
            for entry in sorted(data_dir.glob(pattern)):
                try:
                    st = entry.stat()
                    files_state.append((entry.name, st.st_mtime_ns, st.st_size))
                except OSError:
                    continue
    runs_state = tuple(
        (str(r["run_id"]), str(r["status"]), r["returncode"]) for r in run_manager.list_runs()
    )
    return (tuple(files_state), runs_state)


def _db_fingerprint(db_path: Path, *, is_running: bool) -> tuple[Any, ...]:
    parts: list[tuple[str, int, int]] = []
    for candidate in (db_path, db_path.with_name(f"{db_path.name}-wal")):
        try:
            st = candidate.stat()
            parts.append((candidate.name, st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    return (tuple(parts), is_running)


def create_app(
    data_dir: Path,
    run_manager: RunManager | None = None,
    *,
    viewer_mode: bool = False,
) -> FastAPI:
    """Create the Target Intelligence & Campaign Monitor FastAPI application."""
    resolved_dir = Path(data_dir)
    resolved_dir.mkdir(parents=True, exist_ok=True)
    manager = run_manager if run_manager is not None else RunManager(resolved_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            manager.shutdown()

    app = FastAPI(
        title="Target Intelligence & Campaign Monitor API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.data_dir = resolved_dir
    app.state.run_manager = manager

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[]
        if viewer_mode
        else ["http://localhost:3000", "http://127.0.0.1:3000", "*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    targets_cache: dict[str, Any] = {"fp": None, "data": None}
    detail_cache: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}

    @app.get("/api/targets")
    async def api_list_targets() -> dict[str, Any]:
        fp = _compute_stream_fingerprint(resolved_dir, manager)
        if targets_cache["fp"] == fp and targets_cache["data"] is not None:
            return dict(targets_cache["data"])
        data = list_targets(resolved_dir, running_run_ids=manager.running_ids())
        if viewer_mode:
            data["offline_replays_available"] = True
        targets_cache["fp"] = fp
        targets_cache["data"] = data
        return data

    @app.get("/api/targets/{run_id}")
    async def api_get_target(run_id: str) -> dict[str, Any]:
        db_path = _resolve_db_path(resolved_dir, run_id)
        is_running = run_id in manager.running_ids()
        fp = _db_fingerprint(db_path, is_running=is_running)
        cached = detail_cache.get(run_id)
        if cached is not None and cached[0] == fp:
            return cached[1]
        try:
            data = get_target_detail(db_path, is_running=is_running)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if len(detail_cache) > 64:
            detail_cache.clear()
        detail_cache[run_id] = (fp, data)
        return data

    @app.get("/api/targets/{run_id}/snapshot/{version}")
    async def api_get_snapshot(run_id: str, version: int) -> dict[str, Any]:
        db_path = _resolve_db_path(resolved_dir, run_id)
        snap = get_snapshot_at_version(db_path, version)
        if snap is None:
            raise HTTPException(
                status_code=404,
                detail=f"Snapshot version {version} not found for {run_id}",
            )
        return snap

    @app.get("/api/targets/{run_id}/assets/{asset_id}")
    async def api_get_asset(run_id: str, asset_id: str) -> FileResponse:
        db_path = _resolve_db_path(resolved_dir, run_id)
        if not ASSET_ID_RE.match(asset_id) or ".." in asset_id:
            raise HTTPException(status_code=400, detail=f"Invalid asset_id: {asset_id!r}")
        asset = _read_asset_readonly(db_path, asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail=f"Asset not found: {asset_id}")
        raw_path, content_type = asset
        local_file = Path(raw_path).resolve()
        allowed_roots = (resolved_dir.resolve(), Path("data").resolve())
        if not any(local_file.is_relative_to(root) for root in allowed_roots):
            raise HTTPException(status_code=403, detail=f"Asset path outside data directory: {asset_id}")
        if not local_file.is_file():
            raise HTTPException(status_code=404, detail=f"Asset file missing on disk: {asset_id}")
        return FileResponse(path=local_file, media_type=content_type)

    async def api_launch_run(req: LaunchRunRequest) -> dict[str, Any]:
        if req.scenario != "practice":
            raise HTTPException(
                status_code=400,
                detail="Only 'practice' scenario runs can be launched from the dashboard.",
            )
        try:
            info = manager.launch(
                target_id=req.target_id,
                mode=req.mode,
                stub_objective=req.stub_objective,
                zero_pacing=req.zero_pacing,
                aggressive=req.aggressive,
                scenario=req.scenario,
                data_dir=resolved_dir,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return info.to_dict()

    async def api_stop_run(run_id: str) -> dict[str, Any]:
        if not RUN_ID_RE.match(run_id):
            raise HTTPException(status_code=400, detail=f"Invalid run_id: {run_id!r}")
        stopped = manager.stop(run_id)
        targets_cache["fp"] = None
        targets_cache["data"] = None
        detail_cache.pop(run_id, None)
        return {"run_id": run_id, "stopped": stopped}

    async def api_eval_status() -> dict[str, Any]:
        return manager.eval_status(resolved_dir)

    async def api_eval_scenarios() -> dict[str, Any]:
        meta = await _fetch_eval_scenario_metadata()
        status = manager.eval_status(resolved_dir)
        return {**meta, **status}

    async def api_launch_eval(req: LaunchEvalRequest) -> dict[str, Any]:
        if not req.confirm:
            raise HTTPException(
                status_code=400,
                detail="Explicit confirmation (confirm=true) is required to launch Eval runs.",
            )

        requested_ids: list[str] = []
        seen: set[str] = set()
        for raw_id in req.target_ids:
            cleaned = raw_id.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                requested_ids.append(cleaned)

        if not requested_ids and req.run_all:
            meta = await _fetch_eval_scenario_metadata()
            if not meta.get("open", True):
                raise HTTPException(
                    status_code=403,
                    detail="Eval scenario is not currently open.",
                )
            st = manager.eval_status(resolved_dir)
            target_counts = st.get("target_counts", {})
            max_per_target = int(st.get("max_per_target", 5))
            requested_ids = [
                str(t["id"])
                for t in meta.get("targets", [])
                if t.get("id") and int(target_counts.get(str(t["id"]), 0)) < max_per_target
            ]

        if not requested_ids:
            raise HTTPException(
                status_code=400,
                detail="No eligible eval targets selected (or all eval targets already ran).",
            )

        status_before = manager.eval_status(resolved_dir)
        if len(requested_ids) > int(status_before["remaining"]):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Requested {len(requested_ids)} eval target(s), but only "
                    f"{status_before['remaining']} of {status_before['max_sessions']} tries remain."
                ),
            )

        launched: list[dict[str, Any]] = []
        for tid in requested_ids:
            try:
                info = manager.launch_eval(
                    target_id=tid,
                    zero_pacing=req.zero_pacing,
                    aggressive=req.aggressive,
                    data_dir=resolved_dir,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            launched.append(info.to_dict())

        targets_cache["fp"] = None
        targets_cache["data"] = None
        return {
            "runs": launched,
            "eval_status": manager.eval_status(resolved_dir),
        }

    def api_create_offline_replay(_request: OfflineReplayRequest) -> dict[str, str]:
        return {"run_id": write_acknowledgment_replay(resolved_dir)}

    if viewer_mode:
        app.post("/api/offline-replays", status_code=201)(api_create_offline_replay)
    else:
        app.post("/api/runs/launch")(api_launch_run)
        app.post("/api/runs/{run_id}/stop")(api_stop_run)
        app.get("/api/eval/status")(api_eval_status)
        app.get("/api/eval/scenarios")(api_eval_scenarios)
        app.post("/api/eval/launch")(api_launch_eval)

    @app.get("/api/runs/active")
    async def api_active_runs() -> dict[str, Any]:
        return {
            "runs": manager.list_runs(),
            "running_ids": sorted(manager.running_ids()),
        }

    @app.get("/api/stream")
    async def api_stream(request: Request) -> StreamingResponse:
        async def _event_stream() -> AsyncIterator[str]:
            last_fp = _compute_stream_fingerprint(resolved_dir, manager)
            last_ping = time.monotonic()
            initial_payload = json.dumps({"type": "connected", "ts": utc_now_iso()})
            yield f"data: {initial_payload}\n\n"
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    await asyncio.sleep(1.0)
                    current_fp = _compute_stream_fingerprint(resolved_dir, manager)
                    if current_fp != last_fp:
                        last_fp = current_fp
                        last_ping = time.monotonic()
                        payload = json.dumps({"type": "update", "ts": utc_now_iso()})
                        yield f"data: {payload}\n\n"
                    elif time.monotonic() - last_ping >= 20.0:
                        last_ping = time.monotonic()
                        yield ": ping\n\n"
            except asyncio.CancelledError:
                return

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Target Intelligence & Campaign Monitor FastAPI backend."
    )
    parser.add_argument(
        "--data-dir", default="data", help="Directory containing campaign *.db files"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument(
        "--viewer",
        action="store_true",
        help="View saved runs and create canned offline replays without campaign controls",
    )
    parser.add_argument(
        "--seed-demo",
        action="store_true",
        help="Seed the 3 demo practice databases before starting the server",
    )
    parser.add_argument(
        "--access-log",
        action="store_true",
        help="Enable verbose per-request uvicorn access logging",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if args.seed_demo:
        seed_demo_data(data_dir)

    app = create_app(data_dir, viewer_mode=args.viewer)
    uvicorn.run(app, host=args.host, port=args.port, access_log=args.access_log)


if __name__ == "__main__":
    main()
