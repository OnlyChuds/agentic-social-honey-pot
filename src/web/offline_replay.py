from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from src.models import Event, TargetInfo
from src.orchestrator.state import StateManager
from src.store.db import Store


def write_acknowledgment_replay(data_dir: Path) -> str:
    """Publish a canned transcript without running agents or sending requests."""
    run_id = f"offline_replay_{uuid4().hex}"
    filename = f"{run_id}.db"
    with TemporaryDirectory(prefix=".offline-replay-", dir=data_dir) as scratch:
        path = Path(scratch) / filename
        with Store(path) as store:
            state = StateManager(store)
            state.configure_run(
                scenario="practice",
                target=TargetInfo(
                    id="offline_fixture",
                    name="Offline acknowledgment replay",
                    role="Synthetic test data · no network",
                ),
                run_no=1,
                session_max_touches=1,
                sessions_allowed=1,
            )
            state.start_session(run_id)
            state.transition_state("RESEARCH", reason="Load canned fixture")
            state.transition_state("ENGAGE", reason="Replay fixture")
            state.record_outbound_turn(channel="email", body="Fixture ping.")
            state.record_target_event(
                {"type": "reply", "body": "Fixture acknowledgment."}, default_channel="email"
            )
            state.transition_state("DONE", reason="Fixture replay completed")
            store.append_event(
                Event(
                    type="system",
                    actor="system",
                    base_version=state.snapshot.version,
                    payload={"kind": "run_exit", "reason": "completed"},
                )
            )
        path.rename(data_dir / filename)
    return run_id
