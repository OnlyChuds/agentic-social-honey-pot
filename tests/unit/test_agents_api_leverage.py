from __future__ import annotations

import json
from typing import Any

import pytest

from src.agents.messaging import (
    MessagingAgent,
    StoryClock,
    resolve_sender_for_channel,
)
from src.agents.profile import ProfileAgent, ProfileProposalPayload
from src.agents.strategy import StrategyAgent, _clamp_plan_to_budget, validate_campaign_plan
from src.models import (
    BrandingManifest,
    CampaignPlan,
    Fact,
    Hook,
    LandingPageSpec,
    Profile,
    Snapshot,
    SystemSenderAlias,
    TargetInfo,
    TouchSpec,
)


class _RecordingLLM:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def complete_json(
        self,
        messages: list[dict[str, Any]],
        response_model: type[Any],
        **_kwargs: Any,
    ) -> Any:
        self.calls.append(messages)
        raw = self.responses.pop(0)
        if isinstance(raw, response_model):
            return raw
        return response_model.model_validate(raw)


def _sample_snapshot() -> Snapshot:
    snap = Snapshot()
    snap.run.scenario = "practice"
    snap.run.target_id = "tom"
    snap.target = TargetInfo(id="tom", name="Tom Mueller", role="Co-CEO", org="revel8")
    snap.campaign.budget.session_max = 10
    snap.campaign.budget.used = 0
    snap.facts = [
        Fact(
            fact_id="f_01",
            claim="Tom Mueller is Co-CEO at revel8",
            category="role",
            source="https://revel8.example/team",
            provenance="osint",
            confidence=0.95,
            osint_grade="A",
        )
    ]
    snap.hooks = [
        Hook(
            hook_id="h_01",
            topic="DACH SaaS summit panel",
            evidence=["f_01"],
            why_leverage="High relevance to enterprise GTM",
            confidence=0.85,
            status="active",
        )
    ]
    snap.profile = Profile(
        style_guide="direct and concise",
        trust_style="verifies concrete logistics",
        overall_confidence=0.8,
    )
    snap.identity = BrandingManifest(
        sender_name="Clara Lindner",
        title="Partner Coordinator",
        company="NordicGrid Events",
        domain="nordicgrid-events.example",
        email="clara@nordicgrid-events.example",
        phone="+49 89 5550123",
        system_senders=[
            SystemSenderAlias(
                alias="portal_alerts",
                display_name="NordicGrid Portal",
                email="noreply@nordicgrid-events.example",
                title="Automated Notification",
            )
        ],
    )
    return snap


@pytest.mark.asyncio
async def test_strategy_forwards_story_time_channels_and_memory() -> None:
    snap = _sample_snapshot()
    plan = CampaignPlan(
        objective="credentials",
        primary_hook="h_01",
        touches=[
            TouchSpec(n=1, channel="email", intent="Warm opener", hook="h_01", escalation=1, email_style="personal_html"),
            TouchSpec(n=2, channel="phone_call", intent="Quick alignment call", hook="h_01", escalation=2),
            TouchSpec(n=3, channel="email", intent="Share portal link", hook="h_01", escalation=3, sender_alias="portal_alerts"),
        ],
        pages=[
            LandingPageSpec(
                pretext="Event Overview",
                fields=["name", "email"],
                trigger_touch=2,
                url_path="/overview",
            ),
            LandingPageSpec(
                pretext="Speaker Portal Sign In",
                fields=["email", "password"],
                trigger_touch=3,
                url_path="/speaker-portal",
            ),
        ],
        pivot_triggers=["2 no-reply touches"],
        give_up="touch 5",
    )
    llm = _RecordingLLM([plan])
    agent = StrategyAgent(llm)  # type: ignore[arg-type]

    prop = await agent.run(
        snap,
        current_story_time="2026-11-24T10:15:00+01:00",
        available_channels=[
            {"id": "email", "kind": "async", "arrives_as": "Email with subject and sender"},
            {"id": "phone_call", "kind": "live", "arrives_as": "Incoming phone call (10 turns)"},
        ],
        target_memory={"sessions_seen": 1, "known_watchouts": ["verifies speaker names"]},
    )

    assert prop.kind == "plan"
    user_payload = json.loads(llm.calls[0][-1]["content"])
    assert user_payload["current_story_time"] == "2026-11-24T10:15:00+01:00"
    assert len(user_payload["available_channels"]) == 2
    assert user_payload["target_memory"]["sessions_seen"] == 1
    assert len(prop.payload["pages"]) == 2


def test_validate_and_clamp_multi_page_plan() -> None:
    snap = _sample_snapshot()
    plan = CampaignPlan(
        objective="credentials",
        primary_hook="h_01",
        touches=[
            TouchSpec(n=1, channel="email", intent="Intro", hook="h_01", escalation=1),
            TouchSpec(n=2, channel="email", intent="Portal", hook="h_01", escalation=2),
        ],
        pages=[
            LandingPageSpec(
                pretext="Portal Login",
                fields=["email", "password"],
                trigger_touch=2,
                url_path="/login",
            )
        ],
    )
    assert validate_campaign_plan(plan, snap, max_touches=5) == []

    bad_plan = plan.model_copy(deep=True)
    bad_plan.pages[0].trigger_touch = 9
    bad_plan.pages[0].fields = ["name", "email"]
    errors = validate_campaign_plan(bad_plan, snap, max_touches=5)
    assert any("pages[0].trigger_touch" in e for e in errors)
    assert any("credential fields" in e for e in errors)

    clamped = _clamp_plan_to_budget(bad_plan, snap, max_touches=2)
    assert clamped.pages[0].trigger_touch == 2
    assert "password" in clamped.pages[0].fields


@pytest.mark.asyncio
async def test_profile_forwards_page_submissions_and_call_transcript() -> None:
    snap = _sample_snapshot()
    payload = ProfileProposalPayload.model_validate(
        {
            "profile_update": {
                "style_guide": "direct, analytical",
                "trust_style": "responds to named contacts",
                "overall_confidence": 0.85,
            },
            "disclosures": [
                {
                    "claim": "Uses internal Okta SSO on prod-eu-central",
                    "evidence_event": "ev_call_1",
                    "confidence": 0.9,
                    "public_or_not": "non_public",
                }
            ],
            "rationale": "Extracted disclosure from live call transcript.",
        }
    )
    llm = _RecordingLLM([payload])
    agent = ProfileAgent(llm)  # type: ignore[arg-type]

    prop = await agent.run(
        snap,
        page_submissions=["Target submitted form on https://nordicgrid.test/intake: stack=Okta"],
        call_transcript=[
            {"speaker": "sender", "text": "Which SSO do you integrate with?"},
            {"speaker": "target", "text": "We run internal Okta SSO on prod-eu-central."},
        ],
        recent_events=[{"event_id": "ev_call_1"}],
    )

    user_payload = json.loads(llm.calls[0][-1]["content"])
    assert "page_submissions" in user_payload
    assert "call_transcript" in user_payload
    assert len(prop.payload["disclosures"]) == 1
    assert prop.payload["disclosures"][0]["public_or_not"] == "non_public"


def test_resolve_sender_for_channel_and_story_clock() -> None:
    snap = _sample_snapshot()
    assert snap.identity is not None

    linkedin_sender = resolve_sender_for_channel(snap.identity, "linkedin_message")
    assert linkedin_sender is not None
    assert "Clara Lindner" in linkedin_sender.name
    assert "Partner Coordinator" in linkedin_sender.name
    assert "NordicGrid" in linkedin_sender.name

    alias_sender = resolve_sender_for_channel(
        snap.identity, "email", sender_alias="portal_alerts"
    )
    assert alias_sender is not None
    assert alias_sender.address == "noreply@nordicgrid-events.example"

    clock = StoryClock(timezone="Europe/Berlin")
    t1 = clock.advance_message(minutes=60)
    t2 = clock.advance_turn(seconds=20)
    assert t2 > t1


@pytest.mark.asyncio
async def test_messaging_compose_live_turn_and_email_style() -> None:
    snap = _sample_snapshot()
    llm = _RecordingLLM(
        [
            {
                "mode": "compose",
                "channel": "phone_call",
                "body": "Thanks for taking a quick minute, Tom — I just sent the summit link over.",
                "facts_used": ["f_01"],
                "hang_up": True,
            }
        ]
    )
    agent = MessagingAgent(llm)  # type: ignore[arg-type]

    turn, proposal = await agent.compose_live_turn(
        snap,
        channel="phone_call",
        persona_says="Sure, send me the link and I'll check it after this meeting.",
        turn_index=2,
        current_story_time="2026-11-24T11:00:00+01:00",
        registered_page_url="https://nordicgrid-events.example/portal",
        auto_rewrite=False,
    )
    assert turn.hang_up is True
    assert proposal.payload["hang_up"] is True
    assert "api_payload" in proposal.payload
