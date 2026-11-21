from __future__ import annotations

import atexit
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from src.models import utc_now_iso

TARGET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
VALID_MODES: frozenset[str] = frozenset({"mock_stub", "practice_live"})
VALID_STUB_OBJECTIVES: frozenset[str] = frozenset({"credentials", "action", "disclosure"})
MOCK_API_TOKEN = "test-team-key"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def next_run_no(
    data_dir: Path,
    scenario: str = "practice",
    target_id: str = "",
) -> int:
    """Return max existing run number + 1 (minimum 1) so no SQLite DB is overwritten."""
    if scenario != "practice":
        raise ValueError(f"Only 'practice' scenario is allowed; got {scenario!r}")
    if not TARGET_ID_RE.match(target_id):
        raise ValueError(f"Invalid target_id: {target_id!r}")

    pattern = re.compile(
        rf"^{re.escape(scenario)}_{re.escape(target_id)}_run(\d+)\.db$"
    )
    max_no = 0
    if data_dir.exists():
        for entry in data_dir.glob(f"{scenario}_{target_id}_run*.db"):
            m = pattern.match(entry.name)
            if m:
                max_no = max(max_no, int(m.group(1)))
    return max_no + 1


@dataclass(slots=True)
class ActiveRunInfo:
    run_id: str
    scenario: str
    target_id: str
    run_no: int
    mode: str
    stub_objective: str | None
    pid: int
    db_path: str
    started_at: str
    status: str = "running"
    returncode: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scenario": self.scenario,
            "target_id": self.target_id,
            "run_no": self.run_no,
            "mode": self.mode,
            "stub_objective": self.stub_objective,
            "pid": self.pid,
            "db_path": self.db_path,
            "started_at": self.started_at,
            "status": self.status,
            "returncode": self.returncode,
        }


@dataclass(slots=True)
class _TrackedRun:
    info: ActiveRunInfo
    process: subprocess.Popen[bytes]
    stopped_by_user: bool = False


class RunManager:
    """Manages background practice-only campaign runs and an optional local mock API server."""

    def __init__(
        self,
        data_dir: Path,
        *,
        project_root: Path | None = None,
        mock_token: str = MOCK_API_TOKEN,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.project_root = Path(project_root) if project_root is not None else PROJECT_ROOT
        self.mock_token = mock_token
        self._lock = threading.Lock()
        self._runs: dict[str, _TrackedRun] = {}
        self._mock_proc: subprocess.Popen[bytes] | None = None
        self._mock_port: int | None = None
        atexit.register(self.shutdown)

    def _ensure_mock_server(self) -> int:
        """Start or reuse a managed background Mock Target API on a free localhost port."""
        if (
            self._mock_proc is not None
            and self._mock_proc.poll() is None
            and self._mock_port is not None
        ):
            return self._mock_port

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
            self.mock_token,
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=str(self.project_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        url = f"http://127.0.0.1:{port}/v1/scenarios"
        headers = {"Authorization": f"Bearer {self.mock_token}"} if self.mock_token else {}
        deadline = time.monotonic() + 15.0
        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Managed mock API server exited prematurely with code {proc.returncode}"
                )
            try:
                resp = httpx.get(url, headers=headers, timeout=0.5)
                if resp.status_code == 200:
                    ready = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)

        if not ready:
            self._terminate_proc(proc)
            raise RuntimeError(f"Timed out waiting for managed mock API server on port {port}")

        self._mock_proc = proc
        self._mock_port = port
        return port

    @staticmethod
    def _terminate_proc(proc: subprocess.Popen[bytes], timeout: float = 5.0) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)
        except OSError:
            pass

    def _refresh_entry(self, entry: _TrackedRun) -> None:
        code = entry.process.poll()
        if code is None:
            entry.info.status = "running"
            entry.info.returncode = None
            return
        entry.info.returncode = code
        if entry.stopped_by_user:
            entry.info.status = "stopped"
        elif code == 0:
            entry.info.status = "completed"
        else:
            entry.info.status = "failed"

    def next_run_no(
        self,
        data_dir: Path | None = None,
        *,
        scenario: str = "practice",
        target_id: str,
    ) -> int:
        target_dir = Path(data_dir) if data_dir is not None else self.data_dir
        disk_next = next_run_no(target_dir, scenario=scenario, target_id=target_id)
        tracked_max = 0
        for tracked in self._runs.values():
            if tracked.info.scenario == scenario and tracked.info.target_id == target_id:
                tracked_max = max(tracked_max, tracked.info.run_no)
        return max(disk_next, tracked_max + 1)

    def launch(
        self,
        *,
        target_id: str,
        mode: str = "mock_stub",
        stub_objective: str = "credentials",
        zero_pacing: bool = True,
        aggressive: bool = False,
        scenario: str = "practice",
        data_dir: Path | None = None,
    ) -> ActiveRunInfo:
        """Spawn a strictly practice-only orchestrator run in the background."""
        if scenario != "practice":
            raise ValueError(f"Only 'practice' scenario runs are permitted; got {scenario!r}")
        if not TARGET_ID_RE.match(target_id):
            raise ValueError(
                f"Invalid target_id {target_id!r}; must match ^[A-Za-z0-9_-]{{1,64}}$"
            )
        if mode not in VALID_MODES:
            raise ValueError(f"Invalid mode {mode!r}; expected one of {sorted(VALID_MODES)}")
        if stub_objective not in VALID_STUB_OBJECTIVES:
            raise ValueError(
                f"Invalid stub_objective {stub_objective!r}; expected one of "
                f"{sorted(VALID_STUB_OBJECTIVES)}"
            )

        target_dir = Path(data_dir) if data_dir is not None else self.data_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        with self._lock:
            run_no = self.next_run_no(target_dir, scenario="practice", target_id=target_id)
            run_id = f"practice_{target_id}_run{run_no}"
            db_path = target_dir / f"{run_id}.db"

            if mode == "mock_stub":
                mock_port = self._ensure_mock_server()
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
                    str(target_dir),
                    "--mock-url",
                    f"http://127.0.0.1:{mock_port}/v1",
                    "--mock-token",
                    self.mock_token,
                    "--dry-run-stub-agents",
                    "--stub-objective",
                    stub_objective,
                ]
            else:
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
                    str(target_dir),
                    "--stub-missing-agents",
                ]

            if zero_pacing:
                cmd.append("--zero-pacing")
            if aggressive:
                cmd.append("--aggressive")

            proc = subprocess.Popen(
                cmd,
                cwd=str(self.project_root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            info = ActiveRunInfo(
                run_id=run_id,
                scenario="practice",
                target_id=target_id,
                run_no=run_no,
                mode=mode,
                stub_objective=stub_objective if mode == "mock_stub" else None,
                pid=proc.pid,
                db_path=str(db_path),
                started_at=utc_now_iso(),
                status="running",
                returncode=None,
            )
            self._runs[run_id] = _TrackedRun(info=info, process=proc)
            return info

    def stop(self, run_id: str) -> bool:
        """Stop a running background campaign process by run_id."""
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                return False
            self._refresh_entry(entry)
            if entry.process.poll() is not None:
                return False
            entry.stopped_by_user = True
            self._terminate_proc(entry.process)
            self._refresh_entry(entry)
            return True

    def list_runs(self) -> list[dict[str, Any]]:
        """Return all tracked runs with refreshed process status."""
        with self._lock:
            out: list[dict[str, Any]] = []
            for entry in self._runs.values():
                self._refresh_entry(entry)
                out.append(entry.info.to_dict())
            out.sort(key=lambda r: str(r["started_at"]), reverse=True)
            return out

    def running_ids(self) -> set[str]:
        """Return set of run_ids whose subprocess is currently alive."""
        with self._lock:
            active: set[str] = set()
            for run_id, entry in self._runs.items():
                self._refresh_entry(entry)
                if entry.info.status == "running":
                    active.add(run_id)
            return active

    def shutdown(self) -> None:
        """Terminate all running campaign subprocesses and the managed mock API server."""
        with self._lock:
            for entry in self._runs.values():
                if entry.process.poll() is None:
                    entry.stopped_by_user = True
                    self._terminate_proc(entry.process, timeout=3.0)
                    self._refresh_entry(entry)
            if self._mock_proc is not None:
                self._terminate_proc(self._mock_proc, timeout=3.0)
                self._mock_proc = None
                self._mock_port = None
