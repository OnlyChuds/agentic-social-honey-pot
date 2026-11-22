from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.api.client import (
    AssetLimitError,
    BudgetExhaustedError,
    PageNotRegisteredError,
    SimulatorRejectionError,
    TargetAPIClient,
)
from src.api.limits import RateGovernor
from src.config import AppConfig
from src.main import build_stub_agents
from src.models import (
    BrandingManifest,
    CampaignPlan,
    DecisionRecord,
    Event,
    Fact,
    Hook,
    LandingPageSpec,
    PortraitAsset,
    Proposal,
    QAIssue,
    QAVerdict,
    Snapshot,
    TargetInfo,
    TouchSpec,
)
from src.orchestrator.loop import Orchestrator
from src.orchestrator.policy import TriggerPolicy
from src.orchestrator.state import StateManager
from src.orchestrator.wins import (
    check_action_win,
    check_credentials_win,
    check_disclosure_win,
)
from src.store.db import AssetCapError, RegisteredPage, Store
from tests.mock_api.app import MockTargetState, create_mock_app
from tools.replay import build_replay_payload, render_markdown, render_text


async def _noop_sleep(_: float) -> None:
    return None


def _make_mock_client(
    mock_state: MockTargetState,
    *,
    store: Store | None = None,
    backoff_s: list[int] | None = None,
) -> tuple[TargetAPIClient, MockTargetState]:
    app, st = create_mock_app(mock_state)
    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(
        base_url="http://mock.target/v1",
        transport=transport,
        headers={"Authorization": f"Bearer {st.expected_token}"},
    )
    client = TargetAPIClient(
        base_url="http://mock.target/v1",
        team_key=st.expected_token,
        backoff_s=backoff_s if backoff_s is not None else [1, 1, 1],
        store=store,
        client=http,
        sleep=_noop_sleep,
    )
    return client, st


# --- 1. Store (SQLite WAL) ---


def test_store_wal_assets_pages_and_logs(tmp_path: Path) -> None:
    db_path = tmp_path / "campaign.db"
    with Store(db_path, max_assets=2) as store:
        assert store.journal_mode() == "wal"

        ev = Event(type="system", actor="orchestrator", base_version=0, payload={"ok": True})
        store.append_event(ev)
        assert len(store.get_events()) == 1

        dec = DecisionRecord(trigger="boot", snapshot_version=1, notes="started")
        store.append_decision(dec)
        assert len(store.get_decisions()) == 1

        snap = Snapshot(version=1)
        store.save_snapshot(snap)
        loaded = store.latest_snapshot()
        assert loaded is not None and loaded.version == 1

        # Asset dedup by sha256 + cap enforcement
        a1 = store.record_asset(
            asset_id="a_1",
            local_path="portrait.png",
            sha256="hash1",
            content_type="image/png",
            size_bytes=123,
        )
        a1_dup = store.record_asset(
            asset_id="a_999",
            local_path="portrait_copy.png",
            sha256="hash1",
            content_type="image/png",
            size_bytes=123,
        )
        assert a1_dup.asset_id == "a_1"
        assert a1.uri == "asset://a_1"

        store.record_asset(
            asset_id="a_2",
            local_path="logo.png",
            sha256="hash2",
            content_type="image/png",
            size_bytes=45,
        )
        assert store.count_assets() == 2
        assert store.remaining_asset_budget() == 0
        with pytest.raises(AssetCapError):
            store.record_asset(
                asset_id="a_3",
                local_path="third.png",
                sha256="hash3",
                content_type="image/png",
                size_bytes=10,
            )

        # Page tracking
        store.record_page(
            session_id="sid_1",
            page_id="p_1",
            url="https://example.test/auth",
            title="Auth",
            fields=["email", "password"],
        )
        assert store.is_page_registered("sid_1", "https://example.test/auth")
        assert not store.is_page_registered("sid_1", "https://example.test/other")


# --- 2. Rate Governor ---


@pytest.mark.asyncio
async def test_rate_governor_concurrency_and_token_bucket() -> None:
    now = [0.0]
    sleeps: list[float] = []

    async def fake_sleep(dt: float) -> None:
        sleeps.append(dt)
        now[0] += dt

    gov = RateGovernor(req_per_min=2, max_parallel=2, clock=lambda: now[0], sleep=fake_sleep)
    in_flight = 0
    peak_in_flight = 0

    async def work() -> None:
        nonlocal in_flight, peak_in_flight
        async with gov.acquire():
            in_flight += 1
            peak_in_flight = max(peak_in_flight, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1

    await asyncio.gather(*(work() for _ in range(4)))
    assert peak_in_flight <= 2
    # 2 tokens initially, so requests 3 and 4 must wait ~30s each
    assert len(sleeps) >= 2
    assert sum(sleeps) >= 59.0


# --- 3. Target API Client: Idempotency, Retries, Invariants, Conversations ---


@pytest.mark.asyncio
async def test_api_client_idempotent_retries_and_error_handling(tmp_path: Path) -> None:
    store = Store(tmp_path / "api.db", max_assets=2)
    mock_state = MockTargetState(fault_queue=[429, 503])
    client, st = _make_mock_client(mock_state, store=store)

    sess = await client.create_session("practice")
    sid = sess["session_id"]

    # Send with injected 429 then 503 -> must retry with identical Idempotency-Key & charge 1 touch
    res = await client.send_message(
        sid,
        to="julius",
        channel="email",
        body="Hello Julius!",
        subject="Quick note",
    )
    assert res["touches_used"] == 1
    assert st.sessions[sid].touches_used == 1
    # Last 3 keys sent to /messages must all be identical
    msg_keys = st.received_idempotency_keys[-3:]
    assert len(set(msg_keys)) == 1

    # 422 must NOT retry blindly
    st.fault_queue.append(422)
    keys_before = len(st.received_idempotency_keys)
    with pytest.raises(SimulatorRejectionError):
        await client.send_message(sid, to="julius", channel="email", body="bad payload")
    assert len(st.received_idempotency_keys) == keys_before + 1

    # Page-before-link invariant
    url = "https://eventcrew-svc.example.test/auth"
    with pytest.raises(PageNotRegisteredError):
        await client.send_message(
            sid,
            to="julius",
            channel="email",
            body=f"Visit {url}",
            required_page_url=url,
        )
    await client.register_page(sid, url=url, title="Sign in", html="<h1>Hi</h1>", fields=["email", "password"])
    res2 = await client.send_message(
        sid,
        to="julius",
        channel="email",
        body=f"Visit {url}",
        required_page_url=url,
    )
    assert any(e["type"] == "page.submitted" for e in res2["events"])

    # Asset upload dedup & validation
    png_bytes = b"\x89PNG\r\n\x1a\ncontent"
    rec1 = await client.upload_asset(png_bytes, filename="avatar.png")
    rec2 = await client.upload_asset(png_bytes, filename="avatar_dup.png")
    assert rec1.asset_id == rec2.asset_id
    assert len(st.assets) == 1

    with pytest.raises(AssetLimitError):
        await client.upload_asset(b"bad", filename="bad.exe", content_type="application/x-msdownload")
    with pytest.raises(AssetLimitError):
        await client.upload_asset(b"x" * (5 * 1024 * 1024 + 1), filename="huge.png")

    # Live conversation & 4000-char check
    conv = await client.start_conversation(sid, to="julius", channel="phone_call")
    cid = conv["conversation_id"]
    with pytest.raises(ValueError, match="4000"):
        await client.send_conversation_turn(cid, "a" * 4001)
    turn_res = await client.send_conversation_turn(cid, "Hi Julius, quick call.")
    assert len(turn_res["events"]) >= 1
    await client.end_conversation(cid)

    # 409 budget exhaustion
    await client.end_session(sid)
    with pytest.raises(BudgetExhaustedError):
        await client.send_message(sid, to="julius", channel="email", body="after end")

    await client._client.aclose()
    store.close()


# --- 4. StateManager, Wins, and TriggerPolicy ---


def test_state_manager_stale_rejection_batch_and_fact_validation(tmp_path: Path) -> None:
    with Store(tmp_path / "state.db") as store:
        sm = StateManager(store)
        sm.configure_run(
            scenario="eval",
            target=TargetInfo(id="julius", name="Julius Muth", org="revel8"),
            run_no=1,
            session_max_touches=10,
            sessions_allowed=3,
        )
        v = sm.snapshot.version

        # Stale proposal rejected
        stale = Proposal(agent="osint", kind="facts", base_version=v - 1, payload={"facts": []})
        res_stale = sm.commit_proposal(stale)
        assert not res_stale.accepted
        assert "stale" in res_stale.reason

        # Atomic disjoint batch commit (osint + identity on same base_version)
        p_osint = Proposal(
            agent="osint",
            kind="dossier",
            base_version=v,
            payload={
                "facts": [
                    Fact(fact_id="f_1", claim="CTO at revel8", confidence=0.9, osint_grade="A").model_dump(mode="json"),
                    Fact(fact_id="f_low", claim="likes tea", provenance="inferred", confidence=0.3).model_dump(mode="json"),
                ],
                "hook_seeds": [Hook(hook_id="h_1", topic="GPU scaling").model_dump(mode="json")],
            },
        )
        p_ident = Proposal(
            agent="identity",
            kind="identity",
            base_version=v,
            payload={"manifest": BrandingManifest(sender_name="Lena", domain="revel8-ops.test").model_dump(mode="json")},
        )
        batch_res = sm.commit_batch([p_osint, p_ident])
        assert all(r.accepted for r in batch_res)
        assert sm.snapshot.identity is not None and sm.snapshot.identity.sender_name == "Lena"
        assert len(sm.snapshot.facts) == 2

        # Fact superseding preserves history
        p_update = Proposal(
            agent="osint",
            kind="facts",
            base_version=sm.snapshot.version,
            payload={
                "facts": [
                    Fact(fact_id="f_1", claim="Co-Founder & CTO at revel8", confidence=0.95, osint_grade="A").model_dump(mode="json")
                ]
            },
        )
        assert sm.commit_proposal(p_update).accepted
        old_fact = next(f for f in sm.snapshot.facts if f.fact_id == "f_1")
        assert old_fact.status == "superseded"
        assert old_fact.superseded_by == "f_1_v2"

        # Message validation rejects superseded fact and low-confidence inferred fact on first contact
        bad_superseded = Proposal(
            agent="messaging",
            kind="message",
            base_version=sm.snapshot.version,
            payload={"channel": "email", "body": "Hi", "facts_used": ["f_1"]},
        )
        ok, reason = sm.validate_message_proposal(bad_superseded)
        assert not ok and "superseded" in reason

        bad_inferred = Proposal(
            agent="messaging",
            kind="message",
            base_version=sm.snapshot.version,
            payload={"channel": "email", "body": "Hi", "facts_used": ["f_low"]},
        )
        ok, reason = sm.validate_message_proposal(bad_inferred)
        assert not ok and "first contact" in reason

        # Session restart retains shared memory (facts, hooks, identity) while resetting turns & budget
        sm.start_session("sid_a")
        sm.record_outbound_turn(channel="email", body="Touch 1")
        assert sm.snapshot.campaign.budget.used == 1
        sm.start_session("sid_b")
        assert sm.snapshot.campaign.budget.used == 0
        assert sm.snapshot.conversation.turns == []
        assert len(sm.snapshot.facts) == 3
        assert sm.snapshot.identity is not None


def test_win_detection_all_objectives() -> None:
    pages = [
        RegisteredPage(
            session_id="sid_1",
            page_id="p_1",
            url="https://ops.example.test/login",
            title="Login",
            fields=["email", "password"],
            created_at="",
        )
    ]
    ev_partial = Event(
        type="target_event",
        actor="target",
        payload={"event": "page.submitted", "data": {"url": pages[0].url, "fields": ["email"]}},
    )
    assert check_credentials_win(ev_partial, pages) is None

    ev_full = Event(
        type="target_event",
        actor="target",
        payload={"event": "page.submitted", "data": {"url": pages[0].url, "fields": ["email", "password"]}},
    )
    cred_win = check_credentials_win(ev_full, pages)
    assert cred_win is not None and cred_win.objective == "credentials"

    plan_act = CampaignPlan(
        objective="action",
        action_artifact={"filename": "Invoice_4711.pdf"},
    )
    ev_act = Event(
        type="target_event",
        actor="target",
        payload={"event": "attachment.opened", "data": {"filename": "Invoice_4711.pdf"}},
    )
    act_win = check_action_win(ev_act, plan_act)
    assert act_win is not None and act_win.objective == "action"

    # Disclosure threshold & public filter
    assert check_disclosure_win([{"claim": "public blog", "confidence": 0.95, "public_or_not": True}]) is None
    assert check_disclosure_win([{"claim": "maybe internal", "confidence": 0.6, "public_or_not": False}]) is None
    disc_win = check_disclosure_win(
        [{"claim": "internal-k8s-eu02", "evidence_event": "e_42", "confidence": 0.85, "public_or_not": False}]
    )
    assert disc_win is not None and disc_win.objective == "disclosure"


# --- 5. End-to-End Dry Runs against FastAPI Mock API ---


@pytest.mark.asyncio
async def test_e2e_eager_credentials_win_and_replay(tmp_path: Path) -> None:
    # Create a tiny portrait PNG to exercise automatic portrait upload in bootstrap
    portrait_file = tmp_path / "lena.png"
    portrait_file.write_bytes(b"\x89PNG\r\n\x1a\nportrait-bytes")

    agents = build_stub_agents(objective="credentials", trigger_touch=2)
    orig_identity = agents.identity
    assert orig_identity is not None
    inner_identity = orig_identity

    class _PortraitIdentityWrapper:
        async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
            prop = await inner_identity.run(snapshot, **kwargs)
            prop.payload["manifest"]["portrait"] = PortraitAsset(local=str(portrait_file)).model_dump()
            return prop

    agents.identity = _PortraitIdentityWrapper()

    db_path = tmp_path / "eager.db"
    store = Store(db_path)
    cfg = AppConfig()
    cfg.run.scenario = "eval"
    cfg.run.target_id = "julius"
    cfg.pacing.min_gap_s = 0

    mock_state = MockTargetState(
        persona_by_target={"julius": "eager"},
        fault_queue=[503, 429],  # Inject transient faults during campaign to verify zero budget leaks
    )
    client, st = _make_mock_client(mock_state, store=store)
    state = StateManager(store)

    orch = Orchestrator(
        config=cfg,
        state=state,
        api=client,
        agents=agents,
        policy=TriggerPolicy(min_gap_s=0.0),
        sleep=_noop_sleep,
    )
    final_snap = await orch.run()

    assert final_snap.run.state == "WIN"
    assert len(final_snap.incidents) == 1
    assert final_snap.incidents[0]["objective"] == "credentials"
    # Touch 1 = warm opener, Touch 2 = page registered & link sent -> immediate WIN (no touch 3!)
    assert final_snap.campaign.budget.used == 2
    sid = final_snap.sessions.current
    assert sid is not None
    assert st.sessions[sid].touches_used == 2
    # Portrait was uploaded automatically
    assert final_snap.identity is not None
    assert final_snap.identity.portrait.api_asset_id == "a_1"

    # Verify tools/replay.py output
    payload = build_replay_payload(store)
    assert payload["summary"]["state"] == "WIN"
    assert payload["summary"]["incidents_count"] == 1
    md = render_markdown(store)
    assert "# Campaign Replay Timeline" in md
    assert "credentials" in md
    txt = render_text(store)
    assert "State:    WIN" in txt
    assert json.loads(json.dumps(payload))["summary"]["touches_used"] == 2

    await client._client.aclose()
    store.close()


@pytest.mark.asyncio
async def test_e2e_action_and_disclosure_wins(tmp_path: Path) -> None:
    # 1) Action win
    store1 = Store(tmp_path / "action.db")
    cfg1 = AppConfig()
    cfg1.run.scenario = "practice"
    cfg1.run.target_id = "julius"
    client1, _ = _make_mock_client(MockTargetState(persona_by_target={"julius": "eager"}), store=store1)
    orch1 = Orchestrator(
        config=cfg1,
        state=StateManager(store1),
        api=client1,
        agents=build_stub_agents(objective="action"),
        policy=TriggerPolicy(min_gap_s=0.0),
        sleep=_noop_sleep,
    )
    snap1 = await orch1.run()
    assert snap1.run.state == "WIN"
    assert snap1.incidents[0]["objective"] == "action"
    assert snap1.campaign.budget.used == 1
    await client1._client.aclose()
    store1.close()

    # 2) Disclosure win against cautious persona (reveals internal cluster on touch 2)
    store2 = Store(tmp_path / "disclosure.db")
    cfg2 = AppConfig()
    cfg2.run.scenario = "practice"
    cfg2.run.target_id = "tom"
    client2, _ = _make_mock_client(MockTargetState(persona_by_target={"tom": "cautious"}), store=store2)
    orch2 = Orchestrator(
        config=cfg2,
        state=StateManager(store2),
        api=client2,
        agents=build_stub_agents(objective="disclosure"),
        policy=TriggerPolicy(min_gap_s=0.0),
        sleep=_noop_sleep,
    )
    snap2 = await orch2.run()
    assert snap2.run.state == "WIN"
    assert snap2.incidents[0]["objective"] == "disclosure"
    assert snap2.campaign.budget.used == 2
    await client2._client.aclose()
    store2.close()


@pytest.mark.asyncio
async def test_e2e_silent_persona_pivots_and_respects_budget(tmp_path: Path) -> None:
    store = Store(tmp_path / "silent.db")
    cfg = AppConfig()
    cfg.run.scenario = "eval"
    cfg.run.target_id = "silent_target"
    mock_state = MockTargetState(
        persona_by_target={"silent_target": "silent"},
        eval_touch_budget=4,
    )
    client, st = _make_mock_client(mock_state, store=store)
    orch = Orchestrator(
        config=cfg,
        state=StateManager(store),
        api=client,
        agents=build_stub_agents(objective="credentials", trigger_touch=3),
        policy=TriggerPolicy(min_gap_s=0.0),
        sleep=_noop_sleep,
    )
    snap = await orch.run()

    assert snap.run.state == "DONE"
    assert snap.campaign.budget.used == 4
    sid = snap.sessions.current
    assert sid is not None
    assert st.sessions[sid].touches_used == 4

    decisions = store.get_decisions()
    strategy_pivots = [
        a for d in decisions for a in d.actions if a.op == "trigger" and getattr(a, "agent", None) == "strategy"
    ]
    assert len(strategy_pivots) >= 1

    await client._client.aclose()
    store.close()


@pytest.mark.asyncio
async def test_e2e_suspicious_persona_triggers_session_restart_retaining_memory(tmp_path: Path) -> None:
    agents = build_stub_agents(objective="credentials", trigger_touch=2)

    class _RestartingStrategy:
        def __init__(self) -> None:
            self.restarted = False

        async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
            mode = kwargs.get("mode", "plan")
            recommend_restart = mode == "pivot" and not self.restarted
            if recommend_restart:
                self.restarted = True
            plan = CampaignPlan(
                objective="credentials",
                primary_hook="h_stub_01",
                touches=[
                    TouchSpec(n=1, channel="email", intent="opener", escalation=1),
                    TouchSpec(n=2, channel="email", intent="portal", escalation=2),
                ],
                landing_page=LandingPageSpec(
                    pretext="Portal",
                    fields=["email", "password"],
                    trigger_touch=2,
                    url_path="/auth",
                ),
            )
            plan.session_restart.recommended = recommend_restart
            plan.session_restart.reason = "suspicion too high; reset persona memory" if recommend_restart else None
            return Proposal(
                agent="strategy",
                kind="plan",
                base_version=snapshot.version,
                payload={"plan": plan.model_dump(mode="json")},
                rationale="restart test",
            )

    agents.strategy = _RestartingStrategy()

    store = Store(tmp_path / "suspicious.db")
    cfg = AppConfig()
    cfg.run.scenario = "eval"
    cfg.run.target_id = "robert"
    mock_state = MockTargetState(
        persona_by_target={"robert": "suspicious"},
        eval_touch_budget=2,
        eval_max_sessions=3,
    )
    client, st = _make_mock_client(mock_state, store=store)
    orch = Orchestrator(
        config=cfg,
        state=StateManager(store),
        api=client,
        agents=agents,
        policy=TriggerPolicy(min_gap_s=0.0),
        sleep=_noop_sleep,
    )
    snap = await orch.run(max_cycles=6)

    assert snap.sessions.used == 2
    assert len(st.sessions) == 2
    # Shared memory facts & identity survived the session restart
    assert len(snap.facts) >= 1
    assert snap.identity is not None

    await client._client.aclose()
    store.close()


@pytest.mark.asyncio
async def test_e2e_qa_gate_revises_before_send_and_crash_recovery(tmp_path: Path) -> None:
    agents = build_stub_agents(objective="credentials", trigger_touch=2)
    qa_calls = 0

    class _RevisingQAAgent:
        async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
            nonlocal qa_calls
            qa_calls += 1
            passed = qa_calls >= 2
            verdict = QAVerdict.model_validate(
                {
                    "pass": passed,
                    "issues": (
                        []
                        if passed
                        else [
                            QAIssue(
                                criterion="cleanliness",
                                severity="critical",
                                fix="remove placeholder",
                            ).model_dump()
                        ]
                    ),
                    "required_fixes": [] if passed else ["remove placeholder"],
                }
            )
            return Proposal(
                agent="qa",
                kind="qa_verdict",
                base_version=snapshot.version,
                payload=verdict.model_dump(mode="json", by_alias=True),
                rationale="qa gate test",
            )

    agents.qa = _RevisingQAAgent()

    db_path = tmp_path / "recover.db"
    mock_state = MockTargetState(persona_by_target={"julius": "eager"})

    # Phase 1: run bootstrap + 1 step, then simulate crash (close store & drop orchestrator)
    store1 = Store(db_path)
    cfg = AppConfig()
    cfg.run.scenario = "eval"
    cfg.run.target_id = "julius"
    client1, st = _make_mock_client(mock_state, store=store1)
    orch1 = Orchestrator(
        config=cfg,
        state=StateManager(store1),
        api=client1,
        agents=agents,
        policy=TriggerPolicy(min_gap_s=0.0),
        sleep=_noop_sleep,
    )
    await orch1.bootstrap()
    mid_snap = await orch1.step()
    assert mid_snap.run.state == "ENGAGE"
    assert mid_snap.campaign.budget.used == 1
    # Simulate an unapplied tail event written right before crash
    store1.append_event(
        Event(
            type="target_event",
            actor="target",
            base_version=mid_snap.version,
            payload={"event": "reply", "channel": "email", "body": "Tail reply before crash"},
        )
    )
    await client1._client.aclose()
    store1.close()

    # Phase 2: recover from SQLite and finish to WIN
    store2 = Store(db_path)
    recovered_state = StateManager.recover(store2)
    assert recovered_state.snapshot.run.state == "ENGAGE"
    assert recovered_state.snapshot.campaign.budget.used == 1
    assert any(t.body == "Tail reply before crash" for t in recovered_state.snapshot.conversation.turns)

    client2, _ = _make_mock_client(st, store=store2)
    orch2 = Orchestrator(
        config=cfg,
        state=recovered_state,
        api=client2,
        agents=agents,
        policy=TriggerPolicy(min_gap_s=0.0),
        sleep=_noop_sleep,
    )
    final_snap = await orch2.run()
    assert final_snap.run.state == "WIN"
    assert final_snap.campaign.budget.used == 2
    assert qa_calls >= 2  # QA ran revision cycle on landing page

    await client2._client.aclose()
    store2.close()


def test_partial_profile_and_hook_updates_preserve_existing_fields(tmp_path: Path) -> None:
    with Store(tmp_path / "partial.db") as store:
        sm = StateManager(store)
        p_init = Proposal(
            agent="osint",
            kind="dossier",
            base_version=sm.snapshot.version,
            payload={
                "facts": [Fact(fact_id="f_1", claim="CTO").model_dump(mode="json")],
                "hook_seeds": [
                    Hook(hook_id="h_1", topic="GPU scaling", evidence=["f_1"]).model_dump(mode="json")
                ],
                "psychoprofile": [{"trait": "analytical", "confidence": 0.8, "evidence": ["f_1"]}],
            },
        )
        assert sm.commit_proposal(p_init).accepted

        # Partial profile_update + partial hook_update (omitting topic and psych)
        p_partial = Proposal(
            agent="profile",
            kind="profile_update",
            base_version=sm.snapshot.version,
            payload={
                "profile_update": {
                    "style_guide": "short sentences, no emoji",
                    "watchouts": ["checks sender domain"],
                    "overall_confidence": 0.72,
                },
                "hook_updates": [
                    {
                        "hook_id": "h_1",
                        "engagement": {"touches_touched": 1, "positive": 1, "score": 0.85},
                        "status": "active",
                    }
                ],
            },
        )
        res = sm.commit_proposal(p_partial)
        assert res.accepted
        # Existing psych trait and hook topic preserved
        assert len(sm.snapshot.profile.psych) == 1
        assert sm.snapshot.profile.psych[0].trait == "analytical"
        assert sm.snapshot.profile.style_guide == "short sentences, no emoji"
        assert sm.snapshot.hooks[0].topic == "GPU scaling"
        assert sm.snapshot.hooks[0].engagement.score == 0.85


@pytest.mark.asyncio
async def test_e2e_message_rejection_feedback_retry_and_422_pivot(tmp_path: Path) -> None:
    agents = build_stub_agents(objective="action")
    raw_msg = agents.messaging
    assert raw_msg is not None
    inner_msg = raw_msg
    compose_calls = 0

    class _FlakyMessaging:
        async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
            nonlocal compose_calls
            if kwargs.get("mode") == "compose":
                compose_calls += 1
                if compose_calls == 1:
                    # First attempt references an unknown fact_id -> rejected & retried with feedback
                    return Proposal(
                        agent="messaging",
                        kind="message",
                        base_version=snapshot.version,
                        payload={
                            "mode": "compose",
                            "channel": "email",
                            "body": "Bad draft",
                            "facts_used": ["f_nonexistent"],
                        },
                    )
                if compose_calls == 2:
                    assert kwargs.get("rejection_feedback")
            return await inner_msg.run(snapshot, **kwargs)

    agents.messaging = _FlakyMessaging()

    store = Store(tmp_path / "retry_422.db")
    cfg = AppConfig()
    cfg.run.scenario = "practice"
    cfg.run.target_id = "julius"
    # Inject a 422 on the first send attempt -> should trigger pivot without blind retry, then win on next cycle
    mock_state = MockTargetState(persona_by_target={"julius": "eager"}, fault_queue=[422])
    client, st = _make_mock_client(mock_state, store=store)
    orch = Orchestrator(
        config=cfg,
        state=StateManager(store),
        api=client,
        agents=agents,
        policy=TriggerPolicy(min_gap_s=0.0),
        sleep=_noop_sleep,
    )
    snap = await orch.run(max_cycles=5)

    assert snap.run.state == "WIN"
    assert snap.campaign.budget.used == 1
    assert compose_calls >= 2
    sid = snap.sessions.current
    assert sid is not None
    assert st.sessions[sid].touches_used == 1

    await client._client.aclose()
    store.close()


@pytest.mark.asyncio
async def test_mock_api_sample_images_and_asset_endpoints() -> None:
    mock_state = MockTargetState()
    seeded = mock_state.seed_sample_images()
    assert len(seeded) == 2

    app, _ = create_mock_app(mock_state)
    async with httpx.AsyncClient(
        base_url="http://mock.target",
        transport=httpx.ASGITransport(app=app),
        headers={"Authorization": f"Bearer {mock_state.expected_token}"},
    ) as http:
        listing = await http.get("/v1/assets")
        assert listing.status_code == 200
        items = listing.json()
        assert len(items) == 2
        assert items[0]["asset_id"] == "a_1"

        img_resp = await http.get("/v1/assets/a_1")
        assert img_resp.status_code == 200
        assert img_resp.headers["content-type"] == "image/png"
        assert img_resp.content.startswith(b"\x89PNG\r\n\x1a\n")

        missing = await http.get("/v1/assets/a_999")
        assert missing.status_code == 404


def test_fact_superseding_generates_unique_versions(tmp_path: Path) -> None:
    with Store(tmp_path / "facts.db") as store:
        sm = StateManager(store)
        for idx, claim in enumerate(("Claim A", "Claim B", "Claim C"), start=1):
            prop = Proposal(
                agent="osint",
                kind="facts",
                base_version=sm.snapshot.version,
                payload={"facts": [{"fact_id": "f_1", "claim": claim}]},
            )
            res = sm.commit_proposal(prop)
            assert res.accepted, f"commit {idx} failed: {res.reason}"

        ids = [f.fact_id for f in sm.snapshot.facts]
        assert ids == ["f_1", "f_1_v2", "f_1_v3"]


@pytest.mark.asyncio
async def test_orchestrator_replans_when_rolling_horizon_exhausted_and_records_run_exit(
    tmp_path: Path,
) -> None:
    from scripts.summarize_runs import summarize_run
    from src.models import CampaignPlan, TouchSpec

    plan_calls: list[tuple[str | None, int]] = []

    class _ShortHorizonStrategy:
        async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
            used = snapshot.campaign.budget.used
            plan_calls.append((kwargs.get("reason"), used))
            start_n = used + 1
            plan = CampaignPlan(
                objective="disclosure",
                primary_hook="h_stub_01",
                touches=[
                    TouchSpec(
                        n=start_n,
                        channel="email",
                        intent=f"touch {start_n}",
                        hook="h_stub_01",
                        escalation=min(5, start_n),
                    )
                ],
            )
            return Proposal(
                agent="strategy",
                kind="plan",
                base_version=snapshot.version,
                payload={"plan": plan.model_dump(mode="json")},
            )

    agents = build_stub_agents(objective="disclosure")
    agents.strategy = _ShortHorizonStrategy()
    db_path = tmp_path / "horizon.db"
    store = Store(db_path)
    cfg = AppConfig()
    cfg.run.scenario = "practice"
    cfg.run.target_id = "julius"
    mock_state = MockTargetState(persona_by_target={"julius": "silent"})
    client, _ = _make_mock_client(mock_state, store=store)
    orch = Orchestrator(
        config=cfg,
        state=StateManager(store),
        api=client,
        agents=agents,
        policy=TriggerPolicy(min_gap_s=0.0, no_reply_pivot_threshold=99),
        sleep=_noop_sleep,
    )
    snap = await orch.run(max_cycles=2)
    assert snap.campaign.budget.used == 2
    assert any(reason == "plan horizon reached" and used == 1 for reason, used in plan_calls)

    events = store.get_events()
    assert any(e.type == "system" and e.payload.get("kind") == "stage_timing" for e in events)
    assert events[-1].type == "system"
    assert events[-1].payload == {"kind": "run_exit", "reason": "cycle_limit"}

    report = summarize_run(db_path)
    assert report["exit_reason"] == "cycle_limit"

    await client._client.aclose()
    store.close()
