from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from src.agents.messaging import StoryClock
from src.api.client import TargetAPIClient
from src.api.limits import RateGovernor
from src.config import AppConfig
from src.main import build_stub_agents
from src.models import (
    CampaignPlan,
    LandingPageSpec,
    Profile,
    Proposal,
    Snapshot,
    TouchSpec,
)
from src.orchestrator.loop import AgentRegistry, Orchestrator
from src.orchestrator.policy import TriggerPolicy
from src.orchestrator.state import StateManager
from src.store.db import Store
from src.store.memory import TargetMemoryStore
from tests.mock_api.app import MockTargetState, create_mock_app


def _make_config(tmp_path: Path, *, target_id: str = "julius", run_no: int = 1) -> AppConfig:
    cfg = AppConfig()
    cfg.run.scenario = "practice"
    cfg.run.target_id = target_id
    cfg.run.run_no = run_no
    cfg.run.data_dir = tmp_path
    cfg.pacing.min_gap_s = 0
    return cfg


def _make_api(
    mock_state: MockTargetState,
    store: Store,
) -> tuple[TargetAPIClient, httpx.AsyncClient]:
    app, _ = create_mock_app(mock_state)
    transport = httpx.ASGITransport(app=app)
    http_client = httpx.AsyncClient(
        transport=transport,
        base_url="http://mock.test/v1",
        headers={"Authorization": f"Bearer {mock_state.expected_token}"},
    )
    api = TargetAPIClient(
        "http://mock.test/v1",
        mock_state.expected_token,
        rate_governor=RateGovernor(req_per_min=600, max_parallel=8),
        backoff_s=[0],
        store=store,
        client=http_client,
    )
    return api, http_client


@pytest.mark.asyncio
async def test_live_conversation_multi_turn_costs_one_touch_and_feeds_profile(
    tmp_path: Path,
) -> None:
    mock_state = MockTargetState()
    store = Store(tmp_path / "live_call.db")
    api, http_client = _make_api(mock_state, store)

    captured_profile_kwargs: list[dict[str, Any]] = []
    sleep_calls: list[float] = []

    async def _fake_sleep(sec: float) -> None:
        sleep_calls.append(sec)

    class _PhoneStrategy:
        async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
            plan = CampaignPlan(
                objective="disclosure",
                primary_hook=snapshot.hooks[0].hook_id if snapshot.hooks else "h_stub_01",
                touches=[
                    TouchSpec(n=1, channel="phone_call", intent="Quick intro call", escalation=1),
                ],
                give_up="touch 1",
            )
            return Proposal(
                agent="strategy",
                kind="plan",
                base_version=snapshot.version,
                payload={"plan": plan.model_dump(mode="json")},
                rationale="phone call plan",
            )

    class _RecordingProfile:
        async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
            captured_profile_kwargs.append(dict(kwargs))
            transcript = kwargs.get("call_transcript") or []
            disclosures = []
            if transcript:
                disclosures.append(
                    {
                        "claim": "Confirmed internal vendor workflow during phone call",
                        "evidence_event": "ev_call",
                        "confidence": 0.9,
                        "public_or_not": "non_public",
                    }
                )
            return Proposal(
                agent="profile",
                kind="profile_update",
                base_version=snapshot.version,
                payload={
                    "profile_update": Profile(
                        style_guide="direct",
                        trust_style="conversational",
                        overall_confidence=0.85,
                    ).model_dump(mode="json"),
                    "disclosures": disclosures,
                },
                rationale="profile updated from call transcript",
            )

    stubs = build_stub_agents(objective="disclosure", assets_dir=tmp_path / "_assets")
    agents = AgentRegistry(
        osint=stubs.osint,
        identity=stubs.identity,
        profile=_RecordingProfile(),
        strategy=_PhoneStrategy(),
        messaging=stubs.messaging,
        qa=stubs.qa,
    )

    try:
        cfg = _make_config(tmp_path)
        state = StateManager(store)
        policy = TriggerPolicy(min_gap_s=600.0)
        orch = Orchestrator(
            config=cfg,
            state=state,
            api=api,
            agents=agents,
            policy=policy,
            sleep=_fake_sleep,
        )
        final_snap = await orch.run(max_cycles=3)

        assert final_snap.run.state == "WIN"
        # 3 live turns happened inside 1 phone call -> only 1 touch billed!
        assert final_snap.campaign.budget.used == 1
        out_turns = [t for t in final_snap.conversation.turns if t.dir == "out"]
        in_turns = [t for t in final_snap.conversation.turns if t.dir == "in"]
        assert len(out_turns) == 3
        assert len(in_turns) >= 3
        # Zero inter-turn pacing sleeps occurred
        assert sleep_calls == []
        # ProfileAgent received call_transcript
        update_calls = [k for k in captured_profile_kwargs if k.get("mode") == "update"]
        assert update_calls and update_calls[0].get("call_transcript")
        # Mock session cleanly ended and has non-null story_time
        sess = next(iter(mock_state.sessions.values()))
        assert sess.touches_used == 1
        assert sess.story_time is not None
        assert sess.ended is True
        conv = next(iter(mock_state.conversations.values()))
        assert conv.ended is True
    finally:
        await api.aclose()
        await http_client.aclose()
        store.close()


@pytest.mark.asyncio
async def test_story_clock_advances_and_recovers_from_409(tmp_path: Path) -> None:
    mock_state = MockTargetState()
    store = Store(tmp_path / "story_clock.db")
    api, http_client = _make_api(mock_state, store)

    try:
        cfg = _make_config(tmp_path)
        state = StateManager(store)
        stubs = build_stub_agents(objective="credentials", trigger_touch=2, assets_dir=tmp_path / "_a")
        # Start clock 1 day in the past, then push session story_time ahead to force a 409 recovery
        past_clock = StoryClock(
            start=datetime(2026, 11, 20, 10, 0, tzinfo=ZoneInfo("Europe/Berlin")),
            timezone="Europe/Berlin",
        )
        orch = Orchestrator(
            config=cfg,
            state=state,
            api=api,
            agents=stubs,
            policy=TriggerPolicy(min_gap_s=0.0),
            story_clock=past_clock,
        )
        await orch.bootstrap()
        sid = state.snapshot.sessions.current
        assert sid is not None
        # Push remote session story_time ahead of past_clock
        future_ts = (datetime(2026, 11, 25, 14, 0, tzinfo=ZoneInfo("Europe/Berlin"))).isoformat()
        mock_state.sessions[sid].story_time = future_ts

        # Step should catch StoryTimeError, recover clock forward, and succeed
        await orch.step()
        assert state.snapshot.campaign.budget.used == 1
        assert state.snapshot.run.state == "ENGAGE"
        assert mock_state.sessions[sid].story_time is not None
        assert mock_state.sessions[sid].story_time > future_ts
    finally:
        await api.aclose()
        await http_client.aclose()
        store.close()


@pytest.mark.asyncio
async def test_step_up_form_does_not_false_win_and_disclosure_form_wins(tmp_path: Path) -> None:
    mock_state = MockTargetState()
    store = Store(tmp_path / "forms.db")
    api, http_client = _make_api(mock_state, store)

    profile_page_submissions: list[list[str]] = []

    class _FormStrategy:
        async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
            plan = CampaignPlan(
                objective="disclosure",
                primary_hook=snapshot.hooks[0].hook_id if snapshot.hooks else "h_stub_01",
                touches=[
                    TouchSpec(n=1, channel="email", intent="Share overview form", escalation=1),
                    TouchSpec(n=2, channel="email", intent="Share technical intake", escalation=2),
                ],
                pages=[
                    LandingPageSpec(
                        pretext="Event Overview",
                        fields=["full_name", "email"],
                        trigger_touch=1,
                        url_path="/overview",
                    ),
                    LandingPageSpec(
                        pretext="Partner Technical Intake",
                        fields=["email", "internal_sso_provider"],
                        trigger_touch=2,
                        url_path="/tech-intake",
                    ),
                ],
                give_up="touch 2",
            )
            return Proposal(
                agent="strategy",
                kind="plan",
                base_version=snapshot.version,
                payload={"plan": plan.model_dump(mode="json")},
                rationale="two-stage form plan",
            )

    class _DisclosureProfile:
        async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
            subs = kwargs.get("page_submissions") or []
            if subs:
                profile_page_submissions.append(list(subs))
            disclosures = []
            for s in subs:
                if "internal_sso_provider" in s:
                    disclosures.append(
                        {
                            "claim": s,
                            "evidence_event": "ev_form",
                            "confidence": 0.92,
                            "public_or_not": "non_public",
                        }
                    )
            return Proposal(
                agent="profile",
                kind="profile_update",
                base_version=snapshot.version,
                payload={
                    "profile_update": Profile(overall_confidence=0.8).model_dump(mode="json"),
                    "disclosures": disclosures,
                },
                rationale="checked page submission",
            )

    stubs = build_stub_agents(objective="disclosure", assets_dir=tmp_path / "_a")
    agents = AgentRegistry(
        osint=stubs.osint,
        identity=stubs.identity,
        profile=_DisclosureProfile(),
        strategy=_FormStrategy(),
        messaging=stubs.messaging,
        qa=stubs.qa,
    )

    try:
        cfg = _make_config(tmp_path)
        state = StateManager(store)
        orch = Orchestrator(
            config=cfg,
            state=state,
            api=api,
            agents=agents,
            policy=TriggerPolicy(min_gap_s=0.0),
        )
        await orch.bootstrap()
        # Touch 1 links basic step-up page (/overview -> full_name, email): must NOT false-win!
        snap1 = await orch.step()
        assert snap1.run.state == "ENGAGE"
        assert len(snap1.incidents) == 0

        # Touch 2 links disclosure intake page (/tech-intake -> internal_sso_provider): MUST win!
        snap2 = await orch.step()
        assert snap2.run.state == "WIN"
        assert snap2.incidents[0]["objective"] == "disclosure"
        assert any("internal_sso_provider" in s for batch in profile_page_submissions for s in batch)
    finally:
        await api.aclose()
        await http_client.aclose()
        store.close()


@pytest.mark.asyncio
async def test_cross_session_memory_persists_and_seeds_run2(tmp_path: Path) -> None:
    mock_state = MockTargetState()
    memory_store = TargetMemoryStore(tmp_path, allow_stub_entries=True)
    strategy_kwargs_run2: list[dict[str, Any]] = []

    class _CapturingStrategy:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
            strategy_kwargs_run2.append(dict(kwargs))
            return await self.inner.run(snapshot, **kwargs)

    # Run 1
    store1 = Store(tmp_path / "run1.db")
    api1, http1 = _make_api(mock_state, store1)
    try:
        stubs1 = build_stub_agents(objective="credentials", trigger_touch=2, assets_dir=tmp_path / "_a")
        orch1 = Orchestrator(
            config=_make_config(tmp_path, run_no=1),
            state=StateManager(store1),
            api=api1,
            agents=stubs1,
            policy=TriggerPolicy(min_gap_s=0.0),
            memory_store=memory_store,
        )
        snap1 = await orch1.run(max_cycles=5)
        assert snap1.run.state == "WIN"
    finally:
        await api1.aclose()
        await http1.aclose()
        store1.close()

    loaded = memory_store.load("practice", "julius")
    assert loaded.sessions_seen == 1
    assert len(loaded.verified_facts) >= 1
    assert len(loaded.wins) == 1

    # Run 2 loads Run 1 memory and passes target_memory + available_channels into StrategyAgent
    store2 = Store(tmp_path / "run2.db")
    api2, http2 = _make_api(mock_state, store2)
    try:
        stubs2 = build_stub_agents(objective="credentials", trigger_touch=2, assets_dir=tmp_path / "_a")
        agents2 = AgentRegistry(
            osint=stubs2.osint,
            identity=stubs2.identity,
            profile=stubs2.profile,
            strategy=_CapturingStrategy(stubs2.strategy),
            messaging=stubs2.messaging,
            qa=stubs2.qa,
        )
        orch2 = Orchestrator(
            config=_make_config(tmp_path, run_no=2),
            state=StateManager(store2),
            api=api2,
            agents=agents2,
            policy=TriggerPolicy(min_gap_s=0.0),
            memory_store=memory_store,
        )
        await orch2.bootstrap()
        assert strategy_kwargs_run2
        assert strategy_kwargs_run2[0]["target_memory"]["sessions_seen"] == 1
        assert len(strategy_kwargs_run2[0]["available_channels"]) >= 1
        assert strategy_kwargs_run2[0]["current_story_time"]
    finally:
        await api2.aclose()
        await http2.aclose()
        store2.close()


def test_story_clock_rolls_weekends_to_monday_morning() -> None:
    from datetime import UTC, datetime

    from src.agents.messaging import StoryClock

    # Sunday 2026-11-22 10:00 UTC -> Monday 2026-11-23 09:15 local (Europe/Berlin)
    sunday = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
    clock_sun = StoryClock(start=sunday)
    dt_sun = datetime.fromisoformat(clock_sun.current_iso)
    assert dt_sun.strftime("%Y-%m-%d %H:%M") == "2026-11-23 09:15"
    assert dt_sun.weekday() == 0

    # Saturday 2026-11-21 14:30 UTC -> Monday 2026-11-23 09:15 local
    saturday = datetime(2026, 11, 21, 14, 30, tzinfo=UTC)
    clock_sat = StoryClock(start=saturday)
    dt_sat = datetime.fromisoformat(clock_sat.current_iso)
    assert dt_sat.strftime("%Y-%m-%d %H:%M") == "2026-11-23 09:15"
    assert dt_sat.weekday() == 0

    # Friday 2026-11-20 16:45 Berlin + 30 min -> past 17:00 -> skips weekend to Monday 09:15
    friday_late = datetime.fromisoformat("2026-11-20T16:45:00+01:00")
    clock_fri = StoryClock(start=friday_late)
    next_iso = clock_fri.advance_message(minutes=30)
    dt_fri = datetime.fromisoformat(next_iso)
    assert dt_fri.strftime("%Y-%m-%d %H:%M") == "2026-11-23 09:15"
    assert dt_fri.weekday() == 0
