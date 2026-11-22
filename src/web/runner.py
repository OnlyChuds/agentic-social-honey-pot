from __future__ import annotations

import atexit
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from src.models import Event, utc_now_iso
from src.store.db import Store

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
        mode: str = "practice_live",
        stub_objective: str = "credentials",
        zero_pacing: bool = True,
        aggressive: bool = True,
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
                ]

            if zero_pacing:
                cmd.append("--zero-pacing")
            if aggressive:
                cmd.append("--aggressive")

            logs_dir = target_dir / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_path = logs_dir / f"{run_id}.log"
            with log_path.open("ab") as log_file:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.project_root),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
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

    @staticmethod
    def _find_external_pids(run_id: str) -> list[int]:
        """Find alive external ``src.main`` PIDs matching ``<scenario>_<target>_run<N>``."""
        m = re.match(r"^(practice|eval)_([A-Za-z0-9_-]+)_run(\d+)$", run_id)
        if not m:
            return []
        scenario, target_id, run_no = m.group(1), m.group(2), m.group(3)
        proc_root = Path("/proc")
        if not proc_root.is_dir():
            return []

        def _flag_val(argv: list[str], flag: str) -> str | None:
            for idx, tok in enumerate(argv):
                if tok == flag and idx + 1 < len(argv):
                    return argv[idx + 1]
                if tok.startswith(f"{flag}="):
                    return tok.split("=", 1)[1]
            return None

        my_pid = os.getpid()
        matched: list[int] = []
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == my_pid:
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if not raw:
                continue
            parts = [p.decode("utf-8", errors="ignore") for p in raw.split(b"\x00") if p]
            if "src.main" not in parts:
                continue

            if (
                _flag_val(parts, "--target") == target_id
                and (_flag_val(parts, "--run-no") or "1") == run_no
                and (_flag_val(parts, "--scenario") or "practice") == scenario
            ):
                matched.append(pid)
        return matched

    @staticmethod
    def _terminate_pid(pid: int, timeout: float = 3.0) -> bool:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        return True

    @staticmethod
    def _mark_db_stopped(db_path: Path) -> bool:
        """Record a clean ``run_exit(stopped)`` event so stopped/orphaned runs never stay 'Live'."""
        if not db_path.is_file():
            return False
        try:
            with Store(db_path) as store:
                latest = store.latest_snapshot()
                events = store.get_events()
                has_exit = any(
                    ev.type == "system" and ev.payload.get("kind") == "run_exit"
                    for ev in events
                )
                is_terminal = latest is not None and latest.run.state in {"WIN", "DONE", "ABORT"}
                if has_exit and is_terminal:
                    return False

                base_ver = latest.version if latest is not None else 0
                if not has_exit:
                    store.append_event(
                        Event(
                            type="system",
                            actor="orchestrator",
                            base_version=base_ver,
                            payload={"kind": "run_exit", "reason": "stopped"},
                        )
                    )
                if latest is not None and latest.run.state not in {"WIN", "DONE", "ABORT"}:
                    latest.run.state = "DONE"
                    latest.version += 1
                    store.save_snapshot(latest)
                return True
        except Exception:  # noqa: BLE001
            return False

    def stop(self, run_id: str) -> bool:
        """Stop a running background or external campaign process and mark its DB stopped."""
        with self._lock:
            terminated = False
            entry = self._runs.get(run_id)
            if entry is not None:
                self._refresh_entry(entry)
                if entry.process.poll() is None:
                    entry.stopped_by_user = True
                    self._terminate_proc(entry.process)
                    self._refresh_entry(entry)
                    terminated = True

            for ext_pid in self._find_external_pids(run_id):
                if self._terminate_pid(ext_pid):
                    terminated = True

            db_marked = self._mark_db_stopped(self.data_dir / f"{run_id}.db")
            return terminated or db_marked

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
