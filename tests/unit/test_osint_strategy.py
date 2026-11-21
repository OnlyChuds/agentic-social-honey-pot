from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.agents.osint import DossierPayload, OrgBranding, OSINTAgent, OsintAgent
from src.agents.profile import (
    DisclosureCandidate,
    HookUpdate,
    ProfileAgent,
    ProfileProposalPayload,
)
from src.agents.strategy import StrategyAgent, validate_campaign_plan
from src.llm.text import TextLLMClient
from src.models import (
    CampaignPlan,
    CampaignView,
    ConversationTurn,
    ConversationView,
    Fact,
    Hook,
    HookEngagement,
    LandingPageSpec,
    Profile,
    PsychTrait,
    RunMeta,
    Snapshot,
    TargetInfo,
    TouchBudget,
    TouchSpec,
)
from src.osint import OsintEngine, OsintResult, Source


def _sample_snapshot(
    *,
    scenario: str = "eval",
    session_max: int = 10,
    used: int = 0,
) -> Snapshot:
    return Snapshot(
        version=5,
        run=RunMeta(scenario="eval" if scenario == "eval" else "practice", target_id="julius"),
        target=TargetInfo(
            id="julius", name="Julius Muth", role="Co-Founder & Co-CEO", org="revel8"
        ),
        facts=[
            Fact(
                fact_id="f_1",
                claim="Co-founder and Co-CEO at revel8 Munich focusing on GTM/sales.",
                category="role",
                source="https://revel8.ai",
                provenance="osint",
                confidence=0.9,
                osint_grade="A",
            ),
            Fact(
                fact_id="f_2",
                claim="Spoke at Munich cybersecurity founder panel on social engineering.",
                category="talks",
                source="https://example.org/talk",
                provenance="osint",
                confidence=0.7,
                osint_grade="B",
            ),
        ],
        hooks=[
            Hook(
                hook_id="h_1",
                topic="Enterprise pilot partnership in DACH",
                evidence=["f_1"],
                why_leverage="GTM focus",
                confidence=0.85,
                engagement=HookEngagement(touches_touched=1, positive=1, score=0.8),
                status="active",
            ),
            Hook(
                hook_id="h_2",
                topic="Generic cold SaaS discount",
                evidence=["f_2"],
                why_leverage="Low relevance",
                confidence=0.3,
                engagement=HookEngagement(touches_touched=2, positive=0, score=0.0),
                status="exhausted",
            ),
        ],
        profile=Profile(
            style_guide="Concise, direct, 2-3 sentences, no emoji.",
            trust_style="Responds to peers and warm intros, skeptical of cold sales.",
            watchouts=["asks for references"],
            overall_confidence=0.75,
        ),
        campaign=CampaignView(budget=TouchBudget(session_max=session_max, used=used)),
        conversation=ConversationView(
            session_id="sid_1",
            turns=[
                ConversationTurn(
                    n=1,
                    dir="in",
                    channel="email",
                    body="Interesting, tell me more.",
                    event_ref="e_001",
                )
            ],
        ),
    )


def test_osint_engine_provider_detection() -> None:
    eng = OsintEngine.from_env({"JINA_API_KEY": "k1", "TAVILY_API_KEY": "k2"})
    assert eng.available_providers() == ["jina", "tavily"]
    assert OsintAgent is OSINTAgent


@pytest.mark.asyncio
async def test_osint_agent_single_synthesis_and_mode_kwarg() -> None:
    eng = OsintEngine(tavily_key="k", max_budget_usd=1.0)
    mock_result = OsintResult(
        sources=[
            Source(
                provider="tavily",
                kind="search",
                label="person",
                url="https://www.revel8.ai/author-julius",
                title="Julius Muth — Co-Founder & CEO",
                text="Julius Muth is Co-Founder & Co-CEO of revel8 in Munich.",
                ok=True,
            )
        ],
        timings={"burst1_s": 1.2, "burst2_s": 0.4, "burst3_s": 0.0, "bursts_s": 1.6},
        budget_usd=0.015,
        org_domain="revel8.ai",
        total_s=1.6,
    )
    eng.run = AsyncMock(return_value=mock_result)  # type: ignore[method-assign]

    llm = TextLLMClient(base_url="https://llm.test/v1", api_key="k")
    dossier = DossierPayload(
        facts=[
            Fact(
                fact_id="f_10",
                claim="Julius Muth is Co-CEO of revel8.",
                category="role",
                source="https://www.revel8.ai/author-julius",
                provenance="osint",
                confidence=0.5,
                osint_grade="A",
            )
        ],
        hook_seeds=[
            Hook(
                hook_id="h_10",
                topic="Enterprise security awareness partnerships",
                evidence=["f_10", "f_missing"],
                why_leverage="GTM priority",
                confidence=0.8,
                status="active",
            )
        ],
        psychoprofile=[PsychTrait(trait="ambitious builder", confidence=0.9, evidence=[])],
        org_branding=OrgBranding(real_domain="", brand_keywords=["revel8", "Munich"]),
        completeness="full",
    )
    llm.complete_json = AsyncMock(return_value=dossier)  # type: ignore[method-assign]

    agent = OsintAgent(llm, engine=eng)
    prop = await agent.run(_sample_snapshot(scenario="practice", session_max=30), mode="bootstrap")
    assert prop.agent == "osint"
    assert prop.kind == "dossier"
    assert prop.base_version == 5
    assert prop.payload["facts"][0]["confidence"] == 0.9
    assert prop.payload["hook_seeds"][0]["status"] == "candidate"
    assert prop.payload["hook_seeds"][0]["evidence"] == ["f_10"]
    assert prop.payload["psychoprofile"][0]["confidence"] == 0.6
    assert prop.payload["org_branding"]["real_domain"] == "revel8.ai"
    assert prop.payload["budget_spent_usd"] == 0.015
    await llm.aclose()


@pytest.mark.asyncio
async def test_profile_agent_invariants() -> None:
    snap = _sample_snapshot()
    llm = TextLLMClient(base_url="https://llm.test/v1", api_key="k")
    raw_payload = ProfileProposalPayload(
        profile_update=Profile(
            style_guide="Direct, 2 sentences.",
            psych=[
                PsychTrait(trait="data-driven", confidence=0.7, evidence=["f_1"]),
                PsychTrait(trait="unanchored-guess", confidence=0.8, evidence=[]),
            ],
            overall_confidence=0.8,
        ),
        hook_updates=[
            HookUpdate(
                hook_id="h_1",
                engagement=HookEngagement(touches_touched=2, positive=0, score=0.1),
                status="active",
                rationale="Deflected twice",
            )
        ],
        new_hooks=[
            Hook(hook_id="h_new_ok", topic="Panel follow-up", evidence=["f_2"], confidence=0.7),
            Hook(
                hook_id="h_new_bad", topic="Hallucinated", evidence=["f_nonexistent"], confidence=0.9
            ),
        ],
        disclosures=[
            DisclosureCandidate(
                claim="Uses internal AWS SSO portal",
                evidence_event="e_001",
                confidence=0.85,
                public_or_not="non_public",
            )
        ],
    )
    llm.complete_json = AsyncMock(return_value=raw_payload)  # type: ignore[method-assign]

    agent = ProfileAgent(llm)
    prop = await agent.run(snap, mode="bootstrap")
    assert prop.agent == "profile"
    assert prop.kind == "profile_update"
    assert prop.payload["hook_updates"][0]["status"] == "exhausted"
    assert [h["hook_id"] for h in prop.payload["new_hooks"]] == ["h_new_ok"]
    assert [t["trait"] for t in prop.payload["profile_update"]["psych"]] == ["data-driven"]
    assert prop.payload["disclosures"][0]["public_or_not"] == "non_public"
    await llm.aclose()


@pytest.mark.asyncio
async def test_strategy_agent_validates_and_repairs_budget_and_hooks() -> None:
    snap = _sample_snapshot(scenario="eval", session_max=10, used=0)
    llm = TextLLMClient(base_url="https://llm.test/v1", api_key="k")

    bad_plan = CampaignPlan(
        objective="credentials",
        primary_hook="h_2",
        touches=[
            TouchSpec(
                n=i,
                channel="email",
                intent=f"touch {i}",
                hook="h_2" if i == 1 else "h_1",
                escalation=min(5, (i + 1) // 2),
            )
            for i in range(1, 12)
        ],
        landing_page=None,
    )
    violations = validate_campaign_plan(bad_plan, snap, max_touches=10)
    assert len(violations) >= 3

    good_plan = CampaignPlan(
        objective="credentials",
        primary_hook="h_1",
        touches=[
            TouchSpec(
                n=i,
                channel="linkedin_message" if i <= 2 else "email",
                intent=f"touch {i}",
                hook="h_1",
                escalation=[1, 2, 2, 3, 3, 3, 4, 4, 5, 5][i - 1],
                stop_if="explicit decline",
            )
            for i in range(1, 11)
        ],
        landing_page=LandingPageSpec(
            pretext="Partner portal login",
            fields=["email", "password"],
            trigger_touch=7,
            url_path="/partner-portal",
        ),
        pivot_triggers=["2 touches no reply", "suspicion >= 0.6"],
        give_up="touch 9 without response",
    )
    assert validate_campaign_plan(good_plan, snap, max_touches=10) == []

    llm.complete_json = AsyncMock(side_effect=[bad_plan, good_plan])  # type: ignore[method-assign]
    agent = StrategyAgent(llm)
    prop = await agent.run(snap, mode="plan")
    assert prop.agent == "strategy"
    assert prop.kind == "plan"
    assert llm.complete_json.call_count == 2
    assert len(prop.payload["touches"]) == 10
    assert prop.payload["primary_hook"] == "h_1"
    assert prop.payload["landing_page"]["trigger_touch"] == 7
    await llm.aclose()


def test_strategy_pivot_blocks_escalation_drop() -> None:
    snap = _sample_snapshot(scenario="eval", session_max=10, used=3)
    snap.campaign.plan = CampaignPlan(
        objective="disclosure",
        primary_hook="h_1",
        touches=[
            TouchSpec(n=1, channel="email", intent="a", hook="h_1", escalation=1),
            TouchSpec(n=2, channel="email", intent="b", hook="h_1", escalation=2),
            TouchSpec(n=3, channel="email", intent="c", hook="h_1", escalation=3),
        ],
    )
    bad_pivot = CampaignPlan(
        objective="disclosure",
        primary_hook="h_1",
        touches=[
            TouchSpec(n=4, channel="linkedin_message", intent="retry", hook="h_1", escalation=1),
        ],
    )
    errs = validate_campaign_plan(bad_pivot, snap, max_touches=7, pivot_reason="2 touches no reply")
    assert any("drops below already-seen escalation 3" in e for e in errs)
