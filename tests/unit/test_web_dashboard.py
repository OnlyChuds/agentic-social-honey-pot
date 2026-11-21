from __future__ import annotations

import asyncio
import hashlib
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from src.store.db import Store
from src.web.demo_seed import seed_demo_data
from src.web.queries import get_snapshot_at_version, get_target_detail, list_targets
from src.web.runner import RunManager, next_run_no
from src.web.server import create_app

EXPECTED_SUMMARY_KEYS: set[str] = {
    "run_id",
    "scenario",
    "target_id",
    "target_name",
    "target_role",
    "target_org",
    "state",
    "touches_used",
    "session_max",
    "sessions_used",
    "sessions_allowed",
    "latest_suspicion",
    "latest_engagement",
    "latest_mood",
    "profile_confidence",
    "incidents_count",
    "sender_name",
    "sender_domain",
    "portrait_asset_id",
    "latest_version",
    "updated_at",
    "is_running",
}

EXPECTED_DETAIL_KEYS: set[str] = {
    "run_id",
    "summary",
    "latest_snapshot",
    "stage_history",
    "signal_series",
    "event_markers",
    "personality_evolution",
    "watchout_history",
    "turns",
    "events",
    "decisions",
    "registered_pages",
    "facts_by_id",
    "assets_by_id",
}

EXPECTED_MILESTONE_KEYS: set[str] = {
    "version",
    "ts",
    "touch_n",
    "label",
    "state",
    "profile",
    "traits",
    "hooks",
    "watchouts_added",
    "suspicion",
    "engagement",
    "mood",
}


def test_seed_demo_data_and_list_targets(tmp_path: Path) -> None:
    seeded = seed_demo_data(tmp_path)
    assert len(seeded) == 3
    assert {p.name for p in seeded} == {
        "practice_julius_run1.db",
        "practice_tom_run1.db",
        "practice_robert_run1.db",
    }

    listing = list_targets(tmp_path, running_run_ids={"practice_tom_run1"})
    assert listing["data_dir"] == str(tmp_path)
    targets = listing["targets"]
    assert len(targets) == 3

    # Verify descending updated_at sort
    timestamps = [t["updated_at"] for t in targets]
    assert timestamps == sorted(timestamps, reverse=True)

    by_id = {t["run_id"]: t for t in targets}
    for summary in targets:
        assert set(summary.keys()) == EXPECTED_SUMMARY_KEYS

    julius = by_id["practice_julius_run1"]
    assert julius["state"] == "WIN"
    assert julius["touches_used"] == 4
    assert julius["incidents_count"] == 1
    assert julius["latest_suspicion"] == pytest.approx(0.12)
    assert julius["latest_engagement"] == pytest.approx(0.91)
    assert julius["is_running"] is False

    tom = by_id["practice_tom_run1"]
    assert tom["state"] == "ENGAGE"
    assert tom["touches_used"] == 3
    assert tom["session_max"] == 30
    assert tom["latest_suspicion"] == pytest.approx(0.39)
    assert tom["is_running"] is True

    robert = by_id["practice_robert_run1"]
    assert robert["state"] == "ENGAGE"
    assert robert["sessions_used"] == 2
    assert robert["sessions_allowed"] == 60
    assert robert["latest_suspicion"] == pytest.approx(0.28)


def test_target_detail_projections_for_all_three_seeded_runs(tmp_path: Path) -> None:
    seed_demo_data(tmp_path)

    # 1) Julius: BOOT -> RESEARCH -> ENGAGE -> WIN with credentials incident
    julius = get_target_detail(tmp_path / "practice_julius_run1.db")
    assert set(julius.keys()) == EXPECTED_DETAIL_KEYS
    stages = [s["stage"] for s in julius["stage_history"]]
    assert stages == ["BOOT", "RESEARCH", "ENGAGE", "WIN"]

    suspicions = [s["suspicion"] for s in julius["signal_series"]]
    engagements = [s["engagement"] for s in julius["signal_series"]]
    assert suspicions == [pytest.approx(0.18), pytest.approx(0.14), pytest.approx(0.12)]
    assert engagements == [pytest.approx(0.65), pytest.approx(0.82), pytest.approx(0.91)]

    marker_kinds = {m["kind"] for m in julius["event_markers"]}
    assert {"register_page", "link.opened", "page.submitted", "incident"}.issubset(marker_kinds)
    assert len(julius["registered_pages"]) == 1
    assert "f_jul_01" in julius["facts_by_id"]
    assert julius["facts_by_id"]["f_jul_01"]["osint_grade"] == "A"
    assert len(julius["turns"]) >= 7

    # Personality evolution checks
    evo_j = julius["personality_evolution"]
    assert len(evo_j) >= 4
    for m in evo_j:
        assert set(m.keys()) == EXPECTED_MILESTONE_KEYS
    latest_traits_j = {t["trait"]: t for t in evo_j[-1]["traits"]}
    assert latest_traits_j["fast_decision_maker"]["delta"] > 0.2
    assert latest_traits_j["fast_decision_maker"]["is_new"] is False
    assert latest_traits_j["frictionless_logistics_bias"]["is_new"] is True

    # 2) Tom: guarded suspicion, evolving watchouts, newly discovered trait at touch 2
    tom = get_target_detail(tmp_path / "practice_tom_run1.db")
    assert [s["suspicion"] for s in tom["signal_series"]] == [
        pytest.approx(0.22),
        pytest.approx(0.44),
        pytest.approx(0.39),
    ]
    watchout_texts = [w["text"] for w in tom["watchout_history"]]
    assert watchout_texts == [
        "asks for specific product references",
        "skeptical of unannounced links",
    ]
    latest_traits_t = {t["trait"]: t for t in tom["personality_evolution"][-1]["traits"]}
    assert latest_traits_t["precision_verification_bias"]["is_new"] is True
    assert latest_traits_t["detail_oriented_product_thinker"]["delta"] > 0.15

    # 3) Robert: suspicion spike >= 0.60, pivot + new_session restart, recovery to 0.28
    robert = get_target_detail(tmp_path / "practice_robert_run1.db")
    assert [s["suspicion"] for s in robert["signal_series"]] == [
        pytest.approx(0.35),
        pytest.approx(0.78),
        pytest.approx(0.28),
    ]
    robert_markers = {m["kind"] for m in robert["event_markers"]}
    assert "pivot" in robert_markers
    assert "new_session" in robert_markers
    assert [w["text"] for w in robert["watchout_history"]] == [
        "challenges sender domain",
        "flags urgency",
    ]
    # Retained OSINT facts and evolved personality across session restart
    assert "f_rob_01" in robert["facts_by_id"]
    latest_traits_r = {t["trait"]: t for t in robert["personality_evolution"][-1]["traits"]}
    assert latest_traits_r["adversarial_header_skepticism"]["confidence"] >= 0.9
    assert latest_traits_r["respects_deep_technical_peers"]["is_new"] is True


def test_get_snapshot_at_version(tmp_path: Path) -> None:
    seed_demo_data(tmp_path)
    db_path = tmp_path / "practice_julius_run1.db"
    snap1 = get_snapshot_at_version(db_path, 1)
    assert snap1 is not None
    assert snap1["version"] == 1
    assert snap1["target"]["id"] == "julius"
    assert get_snapshot_at_version(db_path, 9999) is None


def test_next_run_no_and_validation(tmp_path: Path) -> None:
    assert next_run_no(tmp_path, "practice", "julius") == 1
    (tmp_path / "practice_julius_run1.db").write_bytes(b"")
    (tmp_path / "practice_julius_run3.db").write_bytes(b"")
    assert next_run_no(tmp_path, "practice", "julius") == 4

    with pytest.raises(ValueError, match="practice"):
        next_run_no(tmp_path, "eval", "julius")
    with pytest.raises(ValueError, match="Invalid target_id"):
        next_run_no(tmp_path, "practice", "../evil")


@pytest.mark.asyncio
async def test_fastapi_read_endpoints_and_security(tmp_path: Path) -> None:
    seed_demo_data(tmp_path)

    # Record a local asset in Julius's DB so we can test /assets/{asset_id}
    asset_file = tmp_path / "portrait.png"
    asset_bytes = b"\x89PNG\r\n\x1a\nfake-png-content"
    asset_file.write_bytes(asset_bytes)
    with Store(tmp_path / "practice_julius_run1.db") as store:
        store.record_asset(
            asset_id="asset_julius_01",
            local_path=asset_file,
            sha256=hashlib.sha256(asset_bytes).hexdigest(),
            content_type="image/png",
            size_bytes=len(asset_bytes),
        )

    manager = RunManager(tmp_path)
    app = create_app(tmp_path, run_manager=manager)
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # List targets
            resp = await client.get("/api/targets")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["targets"]) == 3

            # Target detail
            resp = await client.get("/api/targets/practice_julius_run1")
            assert resp.status_code == 200
            detail = resp.json()
            assert detail["run_id"] == "practice_julius_run1"
            assert detail["summary"]["state"] == "WIN"

            # Snapshot at version
            resp = await client.get("/api/targets/practice_julius_run1/snapshot/1")
            assert resp.status_code == 200
            assert resp.json()["version"] == 1

            # Missing snapshot version -> 404
            resp = await client.get("/api/targets/practice_julius_run1/snapshot/9999")
            assert resp.status_code == 404

            # Missing target -> 404
            resp = await client.get("/api/targets/practice_nonexistent_run1")
            assert resp.status_code == 404

            # Path traversal attempts -> rejected (400 on invalid single segment, 400/404 on slash-encoded)
            resp = await client.get("/api/targets/..%2Fetc%2Fpasswd")
            assert resp.status_code in {400, 404}
            for bad_id in ("foo..bar", "practice_julius_run1.db", "bad%5Cpath"):
                resp = await client.get(f"/api/targets/{bad_id}")
                assert resp.status_code == 400

            # Asset serving
            resp = await client.get("/api/targets/practice_julius_run1/assets/asset_julius_01")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("image/png")
            assert resp.content == asset_bytes

            # Missing asset -> 404
            resp = await client.get("/api/targets/practice_julius_run1/assets/missing_asset")
            assert resp.status_code == 404

            # Launch rejects eval scenario with HTTP 400
            resp = await client.post(
                "/api/runs/launch",
                json={
                    "target_id": "julius",
                    "mode": "mock_stub",
                    "scenario": "eval",
                },
            )
            assert resp.status_code == 400

            # Launch rejects unsafe target_id with HTTP 400
            resp = await client.post(
                "/api/runs/launch",
                json={
                    "target_id": "../bad",
                    "mode": "mock_stub",
                    "scenario": "practice",
                },
            )
            assert resp.status_code == 400

        # SSE /api/stream emits text/event-stream update chunk and exits cleanly on disconnect
        sent_messages: list[dict[str, Any]] = []
        disconnected = asyncio.Event()

        async def _receive() -> MutableMapping[str, Any]:
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def _send(message: MutableMapping[str, Any]) -> None:
            sent_messages.append(dict(message))
            if message.get("type") == "http.response.body" and message.get("body"):
                disconnected.set()

        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/stream",
            "raw_path": b"/api/stream",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
        }
        await asyncio.wait_for(app(scope, _receive, _send), timeout=5.0)
        start_msg = next(m for m in sent_messages if m["type"] == "http.response.start")
        assert start_msg["status"] == 200
        headers_dict = {k.decode().lower(): v.decode() for k, v in start_msg["headers"]}
        assert headers_dict["content-type"].startswith("text/event-stream")
        body_chunks = b"".join(
            m.get("body", b"") for m in sent_messages if m["type"] == "http.response.body"
        ).decode()
        assert 'data: {"type": "connected"' in body_chunks
    finally:
        manager.shutdown()


@pytest.mark.asyncio
async def test_launch_real_mock_stub_practice_run_to_win(tmp_path: Path) -> None:
    manager = RunManager(tmp_path)
    app = create_app(tmp_path, run_manager=manager)
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            launch_resp = await client.post(
                "/api/runs/launch",
                json={
                    "target_id": "julius",
                    "mode": "mock_stub",
                    "stub_objective": "credentials",
                    "zero_pacing": True,
                    "aggressive": False,
                    "scenario": "practice",
                },
            )
            assert launch_resp.status_code == 200
            launched = launch_resp.json()
            run_id = launched["run_id"]
            assert run_id == "practice_julius_run1"
            assert launched["scenario"] == "practice"
            assert launched["mode"] == "mock_stub"

            # Wait up to 20s for the stub run against local mock API to finish cleanly
            final_run_info: dict[str, Any] | None = None
            for _ in range(100):
                active_resp = await client.get("/api/runs/active")
                assert active_resp.status_code == 200
                runs = active_resp.json()["runs"]
                matched = next((r for r in runs if r["run_id"] == run_id), None)
                if matched is not None and matched["status"] != "running":
                    final_run_info = matched
                    break
                await asyncio.sleep(0.2)

            assert final_run_info is not None, "mock_stub run did not finish within timeout"
            assert final_run_info["status"] == "completed", f"Run failed: {final_run_info}"
            assert final_run_info["returncode"] == 0

            # Verify completed run appears in /api/targets and reached WIN
            targets_resp = await client.get("/api/targets")
            assert targets_resp.status_code == 200
            targets = targets_resp.json()["targets"]
            assert len(targets) == 1
            summary = targets[0]
            assert summary["run_id"] == run_id
            assert summary["state"] == "WIN"
            assert summary["incidents_count"] >= 1
            assert summary["is_running"] is False

            # Verify detail endpoint projects the real orchestrator DB cleanly
            detail_resp = await client.get(f"/api/targets/{run_id}")
            assert detail_resp.status_code == 200
            detail = detail_resp.json()
            assert detail["summary"]["state"] == "WIN"
            assert detail["stage_history"][-1]["stage"] == "WIN"
            assert len(detail["registered_pages"]) >= 1
            assert len(detail["personality_evolution"]) >= 2

            # Calling stop on an already completed run returns stopped=False cleanly
            stop_resp = await client.post(f"/api/runs/{run_id}/stop")
            assert stop_resp.status_code == 200
            assert stop_resp.json() == {"run_id": run_id, "stopped": False}
    finally:
        manager.shutdown()


@pytest.mark.asyncio
async def test_inline_images_attachments_and_asset_serving(tmp_path: Path) -> None:
    seed_demo_data(tmp_path)
    detail = get_target_detail(tmp_path / "practice_julius_run1.db")

    assert detail["summary"]["portrait_asset_id"] == "a_1"
    assets = detail["assets_by_id"]
    assert "a_1" in assets and assets["a_1"]["is_image"] and assets["a_1"]["exists"]
    assert "a_2" in assets and assets["a_2"]["is_image"] and assets["a_2"]["exists"]

    html_turns = [t for t in detail["turns"] if t.get("is_html")]
    assert len(html_turns) == 1
    html_turn = html_turns[0]
    assert len(html_turn["inline_images"]) == 1
    assert html_turn["inline_images"][0]["asset_id"] == "a_2"
    assert html_turn["inline_images"][0]["url"] == "/api/targets/practice_julius_run1/assets/a_2"

    att_kinds = {a["kind"]: a for a in html_turn["attachments"]}
    assert "image" in att_kinds and att_kinds["image"]["asset_id"] == "a_2"
    assert "virtual" in att_kinds and att_kinds["virtual"]["filename"] == "Munich_Roundtable_Schedule.pdf"

    manager = RunManager(tmp_path)
    app = create_app(tmp_path, run_manager=manager)
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for aid in ("a_1", "a_2"):
                resp = await client.get(f"/api/targets/practice_julius_run1/assets/{aid}")
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("image/png")
                assert resp.content.startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        manager.shutdown()
