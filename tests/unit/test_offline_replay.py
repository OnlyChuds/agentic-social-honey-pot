from pathlib import Path

from fastapi.testclient import TestClient

from scripts.summarize_runs import summarize_run
from src.web.server import create_app


def test_viewer_creates_a_fresh_offline_replay_and_lists_it(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path, viewer_mode=True)) as client:
        initial = client.get("/api/targets").json()
        assert initial["offline_replays_available"] is True
        assert initial["targets"] == []

        fixture = {"fixture": "acknowledgment"}
        first = client.post("/api/offline-replays", json=fixture)
        second = client.post("/api/offline-replays", json=fixture)
        assert first.status_code == second.status_code == 201
        first_id = first.json()["run_id"]
        second_id = second.json()["run_id"]
        assert first_id != second_id

        assert {t["run_id"] for t in client.get("/api/targets").json()["targets"]} == {
            first_id,
            second_id,
        }
        detail = client.get(f"/api/targets/{first_id}").json()
        assert detail["summary"]["state"] == "DONE"
        report = summarize_run(tmp_path / f"{first_id}.db")
        assert report["exit_reason"] == "completed"
        assert report["outbound_turns"] == report["replies"] == 1
        assert report["incident_records"] == 0


def test_viewer_has_no_campaign_controls_or_arbitrary_replay_inputs(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path, viewer_mode=True)) as client:
        assert client.post("/api/runs/launch", json={}).status_code == 404
        assert client.post("/api/runs/example/stop").status_code == 404
        assert client.post("/api/offline-replays", json={"fixture": "unknown"}).status_code == 422
        assert (
            client.post(
                "/api/offline-replays",
                json={"fixture": "acknowledgment", "target_id": "unexpected"},
            ).status_code
            == 422
        )
        assert not list(tmp_path.glob("*.db"))
