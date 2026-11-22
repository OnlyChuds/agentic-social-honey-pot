from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from src.api.client import (
    BudgetExhaustedError,
    ConversationEndedError,
    StoryTimeError,
    TargetAPIClient,
)
from src.store.asset_cache import TeamAssetCache
from src.store.db import Store
from tests.mock_api.app import MockTargetState, create_mock_app


async def _noop_sleep(_s: float) -> None:
    return None


@pytest.mark.asyncio
async def test_list_sessions_unwraps_dict_and_502_504_retry_preserves_idempotency() -> None:
    seen_keys: list[str] = []
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/v1/sessions" and request.method == "GET":
            return httpx.Response(200, json={"sessions": [{"session_id": "s_1"}, {"session_id": "s_2"}]})
        if request.url.path == "/v1/sessions/s_1/messages":
            attempts += 1
            seen_keys.append(request.headers.get("Idempotency-Key", ""))
            if attempts == 1:
                return httpx.Response(502, json={"detail": "Bad gateway"})
            if attempts == 2:
                return httpx.Response(504, json={"detail": "Gateway timeout"})
            return httpx.Response(200, json={"message_id": "m_1", "events": []})
        return httpx.Response(404)

    http = httpx.AsyncClient(base_url="https://target.test/v1", transport=httpx.MockTransport(handler))
    async with TargetAPIClient(
        "https://target.test/v1",
        "key",
        backoff_s=[1, 1, 1],
        client=http,
        sleep=_noop_sleep,
    ) as client:
        sessions = await client.list_sessions()
        assert [s["session_id"] for s in sessions] == ["s_1", "s_2"]

        res = await client.send_message("s_1", to="julius", channel="email", body="Hello")
        assert res["message_id"] == "m_1"
        assert attempts == 3
        assert len(set(seen_keys)) == 1 and seen_keys[0] != ""
    await http.aclose()


@pytest.mark.asyncio
async def test_409_disambiguation_and_channels_caching_and_page_overwrite(tmp_path: Path) -> None:
    mock_state = MockTargetState(persona_by_target={"julius": "eager"})
    app, st = create_mock_app(mock_state)
    http = httpx.AsyncClient(
        base_url="http://mock.target/v1",
        transport=httpx.ASGITransport(app=app),
        headers={"Authorization": f"Bearer {mock_state.expected_token}"},
    )
    with Store(tmp_path / "run1.db") as store:
        client = TargetAPIClient(
            "http://mock.target/v1",
            mock_state.expected_token,
            store=store,
            client=http,
            sleep=_noop_sleep,
        )
        # 1) get_channels() fetches once and caches
        ch1 = await client.get_channels()
        assert any(c["id"] == "phone_call" and c["is_live"] for c in ch1)
        assert any(c["id"] == "mfa_push" for c in ch1)
        ch2 = await client.get_channels()
        assert len(ch2) == len(ch1)

        # 2) StoryTimeError on backwards sent_at does NOT burn a touch
        sess = await client.create_session("practice")
        sid = sess["session_id"]
        await client.send_message(
            sid,
            to="julius",
            channel="email",
            body="First message",
            sent_at="2026-11-23T11:00:00+01:00",
        )
        assert st.sessions[sid].touches_used == 1
        with pytest.raises(StoryTimeError):
            await client.send_message(
                sid,
                to="julius",
                channel="email",
                body="Backwards message",
                sent_at="2026-11-23T10:00:00+01:00",
            )
        assert st.sessions[sid].touches_used == 1
        sess_info = await client.get_session(sid)
        assert sess_info["story_time"] == "2026-11-23T11:00:00+01:00"

        # 3) Plain-form register_page (text/images) + in-place URL overwrite
        url = "https://portal.test/intake"
        await client.register_page(
            sid,
            url=url,
            title="Intake v1",
            text="Please confirm your slot",
            images=["a_1"],
            fields=["email", "preferred_slot"],
        )
        await client.register_page(
            sid,
            url=url,
            title="Portal v2",
            html="<!doctype html><html><body><form><input name='email'><input name='password'></form></body></html>",
            fields=["email", "password"],
            force=True,
        )
        assert len(st.sessions[sid].pages) == 1
        assert st.sessions[sid].pages[0]["fields"] == ["email", "password"]

        # 4) Live conversation returns persona_says/ended/turns_used + ConversationEndedError after end
        conv = await client.start_conversation(
            sid,
            to="julius",
            channel="phone_call",
            sent_at="2026-11-23T12:00:00+01:00",
        )
        assert conv["persona_says"]
        assert conv["ended"] is False
        assert conv["turns_used"] == 0
        cid = conv["conversation_id"]
        turn_res = await client.send_conversation_turn(
            cid,
            f"Check {url} right now",
            sent_at="2026-11-23T12:01:00+01:00",
        )
        assert turn_res["persona_says"]
        assert turn_res["turns_used"] == 1
        page_ev = next(e for e in turn_res["events"] if e["type"] == "page.submitted")
        assert page_ev["values"]["password"] == "(entered, not shown)"
        assert page_ev["values"]["email"] == "julius@revel8.ai"
        with pytest.raises(ConversationEndedError):
            await client.send_conversation_turn(cid, "Still there?")

        # 5) Ended session raises BudgetExhaustedError
        await client.end_session(sid)
        with pytest.raises(BudgetExhaustedError):
            await client.send_message(sid, to="julius", channel="email", body="After end")
    await http.aclose()


@pytest.mark.asyncio
async def test_team_asset_cache_reuses_uploaded_asset_across_separate_stores(tmp_path: Path) -> None:
    mock_state = MockTargetState()
    app, st = create_mock_app(mock_state)
    http = httpx.AsyncClient(
        base_url="http://mock.target/v1",
        transport=httpx.ASGITransport(app=app),
        headers={"Authorization": f"Bearer {mock_state.expected_token}"},
    )
    img_path = tmp_path / "logo.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\nfake-team-asset-bytes")
    cache = TeamAssetCache(tmp_path / "team_assets.json")

    with Store(tmp_path / "run1.db") as store1:
        c1 = TargetAPIClient("http://mock.target/v1", mock_state.expected_token, store=store1, asset_cache=cache, client=http, sleep=_noop_sleep)
        rec1 = await c1.upload_asset(img_path)
        assert len(st.assets) == 1

    with Store(tmp_path / "run2.db") as store2:
        c2 = TargetAPIClient("http://mock.target/v1", mock_state.expected_token, store=store2, asset_cache=cache, client=http, sleep=_noop_sleep)
        rec2 = await c2.upload_asset(img_path)
        assert rec2.asset_id == rec1.asset_id
        # Second upload hit TeamAssetCache and did NOT burn another team asset slot
        assert len(st.assets) == 1
        assert store2.get_asset(rec1.asset_id) is not None

    await http.aclose()
