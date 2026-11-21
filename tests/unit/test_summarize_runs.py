import json
import sqlite3
from pathlib import Path

import pytest

from scripts.summarize_runs import main, summarize_run
from src.models import Event, Snapshot
from src.store.db import Store


def test_summarize_run_reads_wal_and_only_emits_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "synthetic run.db"
    private = "not-for-export@example.test"
    with Store(path) as store:
        snapshot = Snapshot()
        snapshot.run.state = "ENGAGE"
        snapshot.campaign.budget.used = 1
        snapshot.sessions.current = private
        store.save_snapshot(snapshot)
        for second, kind, actor, payload in [
            (0, "session", "orchestrator", {"action": "started", "session_id": private}),
            (5, "agent_output", "qa", {"kind": "qa_verdict", "payload": {"pass": False}}),
            (6, "agent_output", "qa", {"kind": "qa_verdict", "payload": {"pass": True}}),
            (7, "agent_output", "messaging", {"kind": "message", "payload": {"body": private}}),
            (8, "agent_output", "messaging", {"direction": "out", "body": private}),
            (9, "target_event", "target", {"event": "reply", "body": private}),
            (10, "target_event", "target", {"event": "synthetic_other", "data": {"id": private}}),
            (12, "system", "orchestrator", {"kind": "agent_error", "error": private}),
        ]:
            store.append_event(
                Event.model_validate(
                    {
                        "ts": f"2026-01-01T00:00:{second:02}Z",
                        "type": kind,
                        "actor": actor,
                        "payload": payload,
                    }
                )
            )

        assert path.with_suffix(".db-wal").exists()
        main([str(path)])

    output = capsys.readouterr().out
    assert private not in output
    (report,) = json.loads(output)
    assert report["last_recorded_state"] == "ENGAGE"
    assert report["terminal_state_recorded"] is False
    assert report["exit_reason"] is None
    assert report["current_session_touches"] == 1
    assert report["outbound_turns"] == 1
    assert report["target_events"] == 2
    assert report["replies"] == 1
    assert report["qa_checks"] == 2
    assert report["qa_failures"] == 1
    assert report["error_records"] == 1
    assert report["error_categories"] == {"unknown": 1}
    assert report["incident_records"] == 0
    assert report["observed_event_span_seconds"] == 12
    assert report["session_start_to_first_outbound_seconds"] == 8


def test_summarize_run_distinguishes_empty_and_aborted_runs(tmp_path: Path) -> None:
    path = tmp_path / "run.db"
    with Store(path) as store:
        empty = summarize_run(path)
        assert empty["last_recorded_state"] is None
        assert empty["terminal_state_recorded"] is False
        assert empty["replies"] == 0
        assert empty["current_session_touches"] is None
        assert empty["observed_event_span_seconds"] is None
        assert empty["session_start_to_first_outbound_seconds"] is None

        snapshot = Snapshot()
        snapshot.run.state = "ABORT"
        store.save_snapshot(snapshot)
        aborted = summarize_run(path)
        assert aborted["last_recorded_state"] == "ABORT"
        assert aborted["terminal_state_recorded"] is True
        assert aborted["exit_reason"] is None
        assert aborted["current_session_touches"] == 0


@pytest.mark.parametrize(
    ("state", "reason", "error", "category", "expected_errors"),
    [
        ("DONE", "completed", None, None, {}),
        ("ENGAGE", "cycle_limit", None, None, {}),
        ("ENGAGE", "operator_stop", None, None, {}),
        (
            "ABORT",
            "service_failure",
            "NotFoundError: Error code: 404 - private",
            None,
            {"http_404": 1},
        ),
        ("ABORT", "service_failure", "APITimeoutError: private", None, {"timeout": 1}),
        ("ABORT", "service_failure", "private", "http_503", {"http_503": 1}),
        ("ABORT", "service_failure", "private", "timeout", {"timeout": 1}),
        ("ABORT", "service_failure", "Error code: 404 - private", "private", {"unknown": 1}),
        ("ABORT", "service_failure", "private reference 404", None, {"unknown": 1}),
    ],
)
def test_summarize_run_reports_exit_reason_and_error_categories(
    tmp_path: Path,
    state: str,
    reason: str,
    error: str | None,
    category: str | None,
    expected_errors: dict[str, int],
) -> None:
    path = tmp_path / "offline-fixture.db"
    with Store(path) as store:
        store.save_snapshot(Snapshot.model_validate({"run": {"state": state}}))
        if error is not None:
            payload = {"kind": "agent_error", "error": error}
            if category is not None:
                payload["category"] = category
            store.append_event(Event(type="system", actor="orchestrator", payload=payload))
        store.append_event(
            Event(type="system", actor="system", payload={"kind": "run_exit", "reason": reason})
        )
        report = summarize_run(path)

    assert report["last_recorded_state"] == state
    assert report["terminal_state_recorded"] is (state != "ENGAGE")
    assert report["exit_reason"] == reason
    assert report["error_records"] == sum(expected_errors.values())
    assert report["error_categories"] == expected_errors
    assert "private" not in json.dumps(report)


def test_summarize_run_does_not_report_invalid_or_stale_exit_marker(tmp_path: Path) -> None:
    path = tmp_path / "resumed-fixture.db"
    with Store(path) as store:
        for reason in ("private", "cycle_limit"):
            store.append_event(
                Event(type="system", actor="system", payload={"kind": "run_exit", "reason": reason})
            )
        assert summarize_run(path)["exit_reason"] == "cycle_limit"
        store.append_event(
            Event(type="session", actor="orchestrator", payload={"action": "started"})
        )
        assert summarize_run(path)["exit_reason"] is None
        store.append_event(
            Event(type="system", actor="system", payload={"kind": "run_exit", "reason": "private"})
        )
        assert summarize_run(path)["exit_reason"] is None


def test_summarize_run_does_not_create_missing_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        summarize_run(missing)
    assert not missing.exists()
