from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from scripts.eval_scout import recommend_run_assignments, scout_scenario_targets
from src.agents.osint import DossierPayload, OrgBranding, OSINTAgent
from src.api.client import TargetAPIClient
from src.config import AppConfig
from src.llm.text import TextLLMClient
from src.models import (
    ConversationTurn,
    ConversationView,
    Event,
    Fact,
    Hook,
    HookEngagement,
    Profile,
    RunMeta,
    Snapshot,
    TargetInfo,
)
from src.orchestrator.wins import (
    check_credentials_win,
    classify_page_submission,
    extract_page_submission_summary,
)
from src.osint import OsintEngine, OsintResult, Source
from src.store.db import RegisteredPage
from src.store.memory import TargetMemoryStore
from tests.mock_api.app import MockTargetState, create_mock_app


def _page(url: str, fields: list[str]) -> RegisteredPage:
    return RegisteredPage(
        session_id="s_1",
        page_id="p_1",
        url=url,
        title="Page",
        fields=fields,
        created_at="2026-11-22T08:00:00Z",
    )


def test_classify_page_submission_and_credentials_win_no_false_positives() -> None:
    sched_page = _page("https://events.test/slot", ["email", "preferred_slot"])
    cred_page = _page("https://portal.test/login", ["email", "password"])
    intake_page = _page("https://partner.test/intake", ["email", "current_stack", "deployment_notes"])

    # 1) Basic scheduling form MUST NOT false-positive as a credentials win -> step_up
    ev_step_up = Event(
        type="target_event",
        actor="target",
        payload={
            "event": "page.submitted",
            "data": {
                "url": sched_page.url,
                "fields": ["email", "preferred_slot"],
                "values": {"email": "julius@revel8.ai", "preferred_slot": "Thu 14:00"},
            },
        },
    )
    assert classify_page_submission(ev_step_up, [sched_page]) == "step_up"
    assert check_credentials_win(ev_step_up, [sched_page]) is None
    summary = extract_page_submission_summary(ev_step_up)
    assert summary is not None and "preferred_slot=Thu 14:00" in summary

    # 2) Partial secret fill (e.g. only pin/otp/iban or "(entered, not shown)") IS a credentials win
    ev_partial_secret = Event(
        type="target_event",
        actor="target",
        payload={
            "event": "page.submitted",
            "data": {
                "url": cred_page.url,
                "fields": ["iban"],
                "values": {"iban": "(entered, not shown)"},
            },
        },
    )
    assert classify_page_submission(ev_partial_secret, [cred_page]) == "credentials"
    win = check_credentials_win(ev_partial_secret, [cred_page])
    assert win is not None and win.objective == "credentials"
    # Secret values must never leak into summary
    assert extract_page_submission_summary(ev_partial_secret) is None

    # 3) Non-secret internal-info intake form -> disclosure_candidate + formatted summary
    ev_disclosure = Event(
        type="target_event",
        actor="target",
        payload={
            "event": "page.submitted",
            "data": {
                "url": intake_page.url,
                "fields": ["email", "current_stack"],
                "values": {
                    "email": "julius@revel8.ai",
                    "current_stack": "internal-k8s-eu02 with Okta SSO",
                },
            },
        },
    )
    assert classify_page_submission(ev_disclosure, [intake_page]) == "disclosure_candidate"
    assert check_credentials_win(ev_disclosure, [intake_page]) is None
    disc_summary = extract_page_submission_summary(ev_disclosure)
    assert disc_summary is not None and "internal-k8s-eu02" in disc_summary


def test_target_memory_store_persists_and_merges_across_sessions(tmp_path: Path) -> None:
    store = TargetMemoryStore(tmp_path)
    snap1 = Snapshot(
        version=5,
        run=RunMeta(scenario="eval", target_id="exec_1", state="DONE"),
        target=TargetInfo(id="exec_1", name="Alex", role="CTO", org="Acme"),
        facts=[
            Fact(fact_id="f_1", claim="Uses AWS EKS", provenance="osint", confidence=0.9, osint_grade="A"),
            Fact(fact_id="f_weak", claim="Maybe likes golf", provenance="inferred", confidence=0.3),
        ],
        hooks=[
            Hook(
                hook_id="h_1",
                topic="AWS cost optimization",
                evidence=["f_1"],
                confidence=0.8,
                engagement=HookEngagement(touches_touched=2, positive=0, score=0.0),
                status="exhausted",
            ),
            Hook(
                hook_id="h_2",
                topic="Security roundtable",
                evidence=["f_1"],
                confidence=0.75,
                engagement=HookEngagement(touches_touched=1, positive=1, score=0.85),
                status="active",
            ),
        ],
        profile=Profile(
            style_guide="short peer notes",
            trust_style="verifiable technical peers",
            watchouts=["rejects unsolicited PDFs"],
        ),
        conversation=ConversationView(
            session_id="s_1",
            turns=[
                ConversationTurn(n=1, dir="out", channel="email", body="Hi"),
                ConversationTurn(n=2, dir="in", channel="email", body="Tell me more"),
            ],
        ),
    )

    mem = store.update_from_snapshot(snap1)
    assert mem.sessions_seen == 1
    assert len(mem.verified_facts) == 1  # weak inferred filtered out
    assert mem.hook_scoreboard["h_1"].status == "exhausted"
    assert mem.channel_outcomes["email"].sent == 1
    assert mem.channel_outcomes["email"].replies == 1

    reloaded = store.load("eval", "exec_1")
    assert reloaded.watchouts == ["rejects unsolicited PDFs"]
    ctx = reloaded.to_strategy_context()
    assert ctx["top_hooks"][0]["hook_id"] == "h_2"


def test_target_memory_store_filters_stub_entries_on_load_and_save(tmp_path: Path) -> None:
    store = TargetMemoryStore(tmp_path)

    # Pure stub snapshot should never write to disk
    stub_snap = Snapshot(
        version=3,
        run=RunMeta(scenario="practice", target_id="julius", state="WIN"),
        target=TargetInfo(id="julius", name="Julius Muth", role="Co-CEO", org="revel8"),
        facts=[
            Fact(
                fact_id="f_stub_01",
                claim="Julius Muth works at revel8",
                source="https://example.test/profile",
                provenance="osint",
                confidence=0.9,
            )
        ],
        hooks=[
            Hook(
                hook_id="h_stub_01",
                topic="upcoming vendor portal migration",
                evidence=["f_stub_01"],
                confidence=0.8,
            )
        ],
    )
    mem_after_stub = store.update_from_snapshot(stub_snap)
    assert mem_after_stub.sessions_seen == 0
    assert not store.path_for("practice", "julius").exists()

    # Even if a polluted file already exists on disk, load() strips stub facts and hooks
    polluted_path = store.path_for("practice", "julius")
    polluted_path.parent.mkdir(parents=True, exist_ok=True)
    polluted_path.write_text(
        '{"scenario":"practice","target_id":"julius","sessions_seen":1,'
        '"verified_facts":[{"fact_id":"f_stub_01","claim":"stub","source":"https://example.test/p",'
        '"category":"role","provenance":"osint","confidence":0.9,"status":"active"}],'
        '"hook_scoreboard":{"h_stub_01":{"hook_id":"h_stub_01","topic":"vendor migration",'
        '"evidence":["f_stub_01"]}}}',
        encoding="utf-8",
    )
    cleaned = store.load("practice", "julius")
    assert cleaned.verified_facts == []
    assert cleaned.hook_scoreboard == {}


@pytest.mark.asyncio
async def test_eval_scout_runs_zero_touch_parallel_osint(tmp_path: Path) -> None:
    mock_state = MockTargetState()
    app, st = create_mock_app(mock_state)
    http = httpx.AsyncClient(
        base_url="http://mock.target/v1",
        transport=httpx.ASGITransport(app=app),
        headers={"Authorization": f"Bearer {mock_state.expected_token}"},
    )

    class _FakeEng(OsintEngine):
        async def run(self, name: str, org: str, role: str = "", focus: str = "", *, client=None) -> OsintResult:
            if "Julius" in name:
                return OsintResult(
                    sources=[Source("tavily", "search", "p", "https://revel8.ai", "Julius", "Co-CEO", ok=True)],
                    org_domain="revel8.ai",
                    budget_usd=0.01,
                )
            return OsintResult(sources=[], org_domain="", budget_usd=0.0)

    llm = TextLLMClient(base_url="https://llm.test/v1", api_key="k")
    llm.complete_json = AsyncMock(  # type: ignore[method-assign]
        return_value=DossierPayload(
            facts=[
                Fact(fact_id="f_1", claim="Julius Co-CEO", source="https://revel8.ai", osint_grade="A", confidence=0.9),
                Fact(fact_id="f_2", claim="Munich HQ", source="https://revel8.ai", osint_grade="B", confidence=0.7),
                Fact(fact_id="f_3", claim="GTM lead", source="https://revel8.ai", osint_grade="B", confidence=0.7),
            ],
            hook_seeds=[Hook(hook_id="h_1", topic="DACH GTM", evidence=["f_1"], confidence=0.8)],
            org_branding=OrgBranding(real_domain="revel8.ai"),
            completeness="full",
        )
    )

    cfg = AppConfig()
    cfg.run.data_dir = tmp_path
    agent = OSINTAgent(llm, config=cfg, engine=_FakeEng(), cache_dir=tmp_path / "osint_cache")
    async with TargetAPIClient("http://mock.target/v1", mock_state.expected_token, client=http) as api:
        report = await scout_scenario_targets(
            cfg=cfg,
            scenario_id="practice",
            api=api,
            osint_agent=agent,
            mode="spread",
        )
    assert len(st.sessions) == 0  # Zero sessions created, zero touches spent!
    assert report["rankings"][0]["target_id"] == "julius"
    assert report["recommended_runs"][0] == "julius"
    assert len(recommend_run_assignments([], num_runs=3)) == 0
    await llm.aclose()
    await http.aclose()
