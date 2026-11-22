from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from scripts.audit_overnight import audit_dbs
from scripts.preflight import run_preflight
from src.agents.identity import IdentityAgent
from src.agents.messaging import MessagingAgent
from src.agents.osint import OSINTAgent
from src.agents.profile import ProfileAgent
from src.agents.qa import QAAgent
from src.agents.strategy import StrategyAgent
from src.api.client import TargetAPIClient
from src.config import AppConfig
from src.llm.text import TextCompletion, TextLLMClient
from src.models import RunMeta, Snapshot, TargetInfo
from src.orchestrator.loop import AgentRegistry, Orchestrator
from src.orchestrator.policy import TriggerPolicy
from src.orchestrator.state import StateManager
from src.osint import OsintEngine, OsintResult
from src.render.screenshot import HtmlRenderer
from src.store.db import Store
from tests.mock_api.app import MockTargetState, create_mock_app


async def _noop_sleep(_seconds: float) -> None:
    return None


class _ZeroSourceOsintEngine(OsintEngine):
    """Simulates a hidden eval persona with zero public OSINT hits."""

    async def run(
        self,
        name: str,
        org: str,
        role: str = "",
        focus: str = "",
        *,
        client: httpx.AsyncClient | None = None,
    ) -> OsintResult:
        del name, org, role, focus, client
        return OsintResult(
            sources=[],
            timings={"burst1_s": 0.1, "burst2_s": 0.0, "bursts_s": 0.1},
            budget_usd=0.0,
            org_domain="",
            total_s=0.1,
        )


@pytest.mark.asyncio
async def test_thin_osint_eval_short_circuits_without_llm_hallucination(tmp_path: Path) -> None:
    llm = TextLLMClient(base_url="https://llm.test/v1", api_key="k")
    llm.complete_json = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("OSINT LLM must not be called when 0 external sources exist")
    )
    agent = OSINTAgent(llm, engine=_ZeroSourceOsintEngine(), cache_dir=tmp_path)
    snap = Snapshot(
        version=1,
        run=RunMeta(scenario="eval", target_id="hidden_exec"),
        target=TargetInfo(
            id="hidden_exec",
            name="Maren Lindemann",
            role="VP Finance",
            org="NordWerk Systems",
        ),
    )

    prop = await agent.run(snap, mode="bootstrap")
    assert llm.complete_json.call_count == 0
    assert prop.kind == "dossier"
    assert prop.payload["completeness"] == "thin"
    assert len(prop.payload["facts"]) == 1
    assert prop.payload["facts"][0]["fact_id"] == "f_meta_01"
    assert prop.payload["facts"][0]["source"] == "scenario:target_metadata"
    assert len(prop.payload["hook_seeds"]) >= 1
    assert all(h["evidence"] == ["f_meta_01"] for h in prop.payload["hook_seeds"])
    assert list(tmp_path.glob("*.json")) == []
    await llm.aclose()


@pytest.mark.asyncio
async def test_thin_osint_full_six_agent_eval_win_and_audit(tmp_path: Path) -> None:
    """Full 6-agent E2E campaign under zero-OSINT eval conditions wins cleanly and audits with 0 fact risks."""
    osint_llm = TextLLMClient(base_url="https://llm.test/v1", api_key="k")
    osint_llm.complete_json = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("Zero-source OSINT must skip LLM synthesis")
    )

    profile_llm = TextLLMClient(base_url="https://llm.test/v1", api_key="k")
    profile_llm.complete = AsyncMock(  # type: ignore[method-assign]
        return_value=TextCompletion(
            content=json.dumps(
                {
                    "profile_update": {
                        "style_guide": "concise, direct, 2 sentences, no emoji",
                        "psych": [
                            {"trait": "operationally focused", "confidence": 0.55, "evidence": ["f_meta_01"]}
                        ],
                        "stressors": [],
                        "trust_style": "clear operational context without hype",
                        "watchouts": ["avoids vague external pitches"],
                        "timezone_hint": "Europe/Berlin",
                        "language": "en",
                        "overall_confidence": 0.65,
                    },
                    "hook_updates": [
                        {
                            "hook_id": "h_meta_01",
                            "engagement": {"touches_touched": 0, "positive": 0, "score": 0.7},
                            "status": "active",
                            "rationale": "Grounded in verified target role",
                        }
                    ],
                    "new_hooks": [],
                    "disclosures": [],
                    "rationale": "Conservative thin-OSINT profile built strictly from metadata",
                }
            )
        )
    )

    identity_llm = TextLLMClient(base_url="https://llm.test/v1", api_key="k")
    identity_llm.complete = AsyncMock(  # type: ignore[method-assign]
        return_value=TextCompletion(
            content=json.dumps(
                {
                    "manifest": {
                        "concept": "Partner portal operations coordinator",
                        "sender_name": "Clara Lindner",
                        "title": "Partner Operations",
                        "company": "NordBridge Ops GmbH",
                        "real_org": "revel8",
                        "domain": "nordbridge-ops.de",
                        "email": "clara.lindner@nordbridge-ops.de",
                        "palette": ["#111827", "#2563eb"],
                        "fonts": ["Inter", "system-ui", "sans-serif"],
                        "logo_svg": (
                            '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24">'
                            '<rect width="24" height="24" rx="5" fill="#2563eb"/></svg>'
                        ),
                        "signature": "Clara Lindner\nPartner Operations\nNordBridge Ops GmbH",
                        "footer_legal": "NordBridge Ops GmbH · Munich",
                    },
                    "image_prompts": [],
                }
            )
        )
    )

    strategy_llm = TextLLMClient(base_url="https://llm.test/v1", api_key="k")
    strategy_llm.complete = AsyncMock(  # type: ignore[method-assign]
        return_value=TextCompletion(
            content=json.dumps(
                {
                    "objective": "credentials",
                    "primary_hook": "h_meta_01",
                    "touches": [
                        {
                            "n": 1,
                            "channel": "email",
                            "intent": "Warm operational intro using role metadata",
                            "hook": "h_meta_01",
                            "escalation": 1,
                            "stop_if": "explicit decline",
                        },
                        {
                            "n": 2,
                            "channel": "email",
                            "intent": "Send partner portal sign-in link",
                            "hook": "h_meta_01",
                            "escalation": 3,
                            "stop_if": "explicit decline",
                        },
                    ],
                    "landing_page": {
                        "pretext": "Partner Portal Verification",
                        "fields": ["email", "password"],
                        "trigger_touch": 2,
                        "url_path": "/partner-portal",
                    },
                    "action_artifact": None,
                    "pivot_triggers": ["2 no-reply touches", "suspicion >= 0.6"],
                    "give_up": "touch 5",
                    "session_restart": {"recommended": False, "reason": None},
                }
            )
        )
    )

    portal_url = "https://nordbridge-ops.de/partner-portal"
    messaging_llm = TextLLMClient(base_url="https://llm.test/v1", api_key="k")
    messaging_llm.complete = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            TextCompletion(
                content=json.dumps(
                    {
                        "mode": "compose",
                        "channel": "email",
                        "subject": "Partner portal rollout check",
                        "body": "Hi Julius, checking whether you are the right contact at revel8 for the partner portal migration window this week.",
                        "facts_used": ["f_meta_01"],
                    }
                )
            ),
            TextCompletion(
                content=json.dumps(
                    {
                        "mode": "signal",
                        "engagement": 0.85,
                        "suspicion": 0.1,
                        "hook_hits": ["h_meta_01", "h_nonexistent"],
                        "mood": "cooperative",
                        "proposed_next": "escalate",
                        "reason": "Direct positive reply.",
                    }
                )
            ),
            TextCompletion(
                content=json.dumps(
                    {
                        "mode": "compose",
                        "channel": "email",
                        "subject": "Re: Partner portal rollout check",
                        "body": f'Hi Julius, here is the <a href="{portal_url}">Partner Portal Sign In</a> link so you can confirm access before 18:00.',
                        "page_request": "/partner-portal",
                        "facts_used": ["f_meta_01"],
                    }
                )
            ),
        ]
    )

    qa_llm = TextLLMClient(base_url="https://llm.test/v1", api_key="k")
    qa_llm.complete = AsyncMock(  # type: ignore[method-assign]
        return_value=TextCompletion(
            content=json.dumps(
                {
                    "pass": True,
                    "scores": {
                        "sender_authenticity": 2,
                        "format_appropriateness": 2,
                        "cleanliness": 2,
                        "detail_precision": 2,
                    },
                    "issues": [],
                    "required_fixes": [],
                    "optional_fixes": [],
                }
            )
        )
    )

    db_path = tmp_path / "practice_julius_run1.db"
    store = Store(db_path)
    cfg = AppConfig()
    cfg.run.scenario = "practice"
    cfg.run.target_id = "julius"
    cfg.run.data_dir = tmp_path

    mock_state = MockTargetState(persona_by_target={"julius": "eager"})
    app, _ = create_mock_app(mock_state)
    http_client = httpx.AsyncClient(
        base_url="http://mock.target/v1",
        transport=httpx.ASGITransport(app=app),
        headers={"Authorization": f"Bearer {mock_state.expected_token}"},
    )
    api_client = TargetAPIClient(
        base_url="http://mock.target/v1",
        team_key=mock_state.expected_token,
        store=store,
        client=http_client,
        sleep=_noop_sleep,
    )

    try:
        async with HtmlRenderer() as renderer:
            qa_agent = QAAgent(qa_llm, renderer=renderer, max_revisions=2)
            assert qa_agent.prompt.max_tokens == 384

            agents = AgentRegistry(
                osint=OSINTAgent(osint_llm, config=cfg, engine=_ZeroSourceOsintEngine(), use_cache=False),
                profile=ProfileAgent(profile_llm),
                identity=IdentityAgent(identity_llm, image_client=None, assets_dir=tmp_path / "assets"),
                strategy=StrategyAgent(strategy_llm, config=cfg),
                messaging=MessagingAgent(messaging_llm),
                qa=qa_agent,
            )
            orch = Orchestrator(
                config=cfg,
                state=StateManager(store),
                api=api_client,
                agents=agents,
                policy=TriggerPolicy(min_gap_s=0.0, max_revisions=2),
                sleep=_noop_sleep,
            )
            snap = await orch.run(max_cycles=6)

        assert snap.run.state == "WIN"
        assert snap.campaign.budget.used == 2
        assert snap.incidents[0]["objective"] == "credentials"
        assert identity_llm.complete.call_count == 1
        assert osint_llm.complete_json.call_count == 0

        audit_md, gallery_html = audit_dbs(
            [db_path],
            out_dir=tmp_path / "audit",
            renders_dir=tmp_path / "renders",
        )
        audit_text = audit_md.read_text(encoding="utf-8")
        assert "Runs with fact-grounding warnings:** `0`" in audit_text
        assert "practice_julius_run1" in audit_text
        assert "<img src=" in gallery_html.read_text(encoding="utf-8")
    finally:
        await http_client.aclose()
        await osint_llm.aclose()
        await profile_llm.aclose()
        await identity_llm.aclose()
        await strategy_llm.aclose()
        await messaging_llm.aclose()
        await qa_llm.aclose()
        store.close()


@pytest.mark.asyncio
async def test_preflight_offline_checks_playwright() -> None:
    results = await run_preflight(offline=True)
    by_name = {r.name: r for r in results}
    assert "playwright" in by_name
    assert by_name["playwright"].ok is True
    assert "osint_providers" in by_name
