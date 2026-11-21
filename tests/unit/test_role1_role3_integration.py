from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from src.agents.identity import IdentityAgent, IdentityOutput
from src.agents.messaging import MessagingAgent
from src.agents.qa import QAAgent
from src.api.client import TargetAPIClient
from src.config import AppConfig
from src.llm.image import ImageClient
from src.llm.text import TextCompletion, TextLLMClient
from src.main import build_stub_agents, discover_agents
from src.models import (
    BrandingManifest,
    CampaignPlan,
    LandingPageSpec,
    PortraitAsset,
    Proposal,
    Snapshot,
    TouchSpec,
)
from src.orchestrator.loop import AgentRegistry, Orchestrator
from src.orchestrator.policy import TriggerPolicy
from src.orchestrator.state import StateManager
from src.render.screenshot import HtmlRenderer
from src.store.db import Store
from src.web.queries import get_target_detail
from tests.mock_api.app import MockTargetState, create_mock_app


async def _noop_sleep(_: float) -> None:
    return None


def _valid_png_bytes(width: int = 48, height: int = 48) -> bytes:
    def _chunk(ctype: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
        return struct.pack("!I", len(data)) + ctype + data + struct.pack("!I", crc)

    rows = b"".join(b"\x00" + (b"\x22\x66\xaa" * width) for _ in range(height))
    ihdr = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(rows))
        + _chunk(b"IEND", b"")
    )


def _make_mock_image_client() -> ImageClient:
    png = _valid_png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "pid_1"})
        if request.url.path == "/history/pid_1":
            return httpx.Response(
                200,
                json={
                    "pid_1": {
                        "status": {"status_str": "success"},
                        "outputs": {
                            "8": {
                                "images": [
                                    {"filename": "portrait.png", "subfolder": "", "type": "output"}
                                ]
                            }
                        },
                    }
                },
            )
        if request.url.path == "/view":
            return httpx.Response(200, content=png)
        return httpx.Response(404)

    http = httpx.AsyncClient(base_url="https://comfy.test", transport=httpx.MockTransport(handler))
    return ImageClient("https://comfy.test", poll_interval_s=0.005, client=http)


def _make_mock_target_client(
    mock_state: MockTargetState,
    *,
    store: Store,
) -> tuple[TargetAPIClient, MockTargetState]:
    app, st = create_mock_app(mock_state)
    http = httpx.AsyncClient(
        base_url="http://mock.target/v1",
        transport=httpx.ASGITransport(app=app),
        headers={"Authorization": f"Bearer {st.expected_token}"},
    )
    client = TargetAPIClient(
        base_url="http://mock.target/v1",
        team_key=st.expected_token,
        backoff_s=[1, 1],
        store=store,
        client=http,
        sleep=_noop_sleep,
    )
    return client, st


_CLEAN_LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="28" height="28" viewBox="0 0 28 28">'
    '<rect width="28" height="28" rx="6" fill="#2563eb"/>'
    '<circle cx="14" cy="14" r="6" fill="#ffffff"/>'
    "</svg>"
)


@pytest.mark.asyncio
async def test_role1_role3_full_campaign_with_real_playwright_qa_and_portrait_upload(
    tmp_path: Path,
) -> None:
    """Full vertical slice: Role 1 Orchestrator + real Role 3 Identity/Messaging/QA/Playwright + Dashboard."""
    identity_llm = TextLLMClient(base_url="https://llm.test/v1", api_key="k")
    messaging_llm = TextLLMClient(base_url="https://llm.test/v1", api_key="k")
    qa_llm = TextLLMClient(base_url="https://llm.test/v1", api_key="k")

    base_manifest = {
        "concept": "DACH summit partner coordinator",
        "sender_name": "Clara Lindner",
        "title": "Partner Coordinator",
        "company": "SummitCrew GmbH",
        "real_org": "revel8",
        "domain": "summitcrew-events.de",
        "email": "clara.lindner@summitcrew-events.de",
        "phone": "+49 89 5550 1234",
        "bio": "Coordinates executive speaker logistics for DACH security summits.",
        "logo_svg": _CLEAN_LOGO_SVG,
        "palette": ["#111827", "#2563eb"],
        "fonts": ["Inter", "system-ui", "sans-serif"],
        "signature": "Clara Lindner\nPartner Coordinator\nSummitCrew GmbH",
        "footer_legal": "SummitCrew GmbH · Munich",
    }

    bad_landing_html = (
        "<!doctype html><html><head><title>Partner Portal</title></head>"
        "<body><h1>Partner Portal TODO</h1>"
        '<form><input name="email"><input name="password"><button>Sign in</button></form>'
        "</body></html>"
    )
    clean_landing_html = (
        "<!doctype html><html><head><title>SummitCrew GmbH — Partner Portal Sign In</title></head>"
        '<body style="font-family:Inter,sans-serif;padding:24px;background:#f9fafb;color:#111827;">'
        f"<div>{_CLEAN_LOGO_SVG}<h1>SummitCrew GmbH — Partner Portal Sign In</h1>"
        '<form><label>Email</label><input name="email" type="email">'
        '<label>Password</label><input name="password" type="password">'
        '<button type="submit">Continue</button></form>'
        "<footer>SummitCrew GmbH · Munich · clara.lindner@summitcrew-events.de</footer>"
        "</div></body></html>"
    )

    identity_responses = [
        # 1) Bootstrap manifest + portrait image prompt
        TextCompletion(
            content=json.dumps(
                {
                    "manifest": base_manifest,
                    "image_prompts": [
                        {
                            "asset": "master_portrait",
                            "prompt": "casual office smartphone photo of a European woman coordinator",
                            "seed": 42,
                            "size": 1024,
                        }
                    ],
                }
            )
        ),
        # 2) Landing page draft #1 (contains TODO -> QA fails deterministically!)
        TextCompletion(
            content=json.dumps(
                {
                    "manifest": base_manifest,
                    "page_html": {
                        "pretext": "Partner Portal Sign In",
                        "title": "Partner Portal Sign In",
                        "html": bad_landing_html,
                        "fields": ["email", "password"],
                    },
                }
            )
        ),
        # 3) Landing page revision #1 (fixes TODO after receiving qa_feedback + previous_html)
        TextCompletion(
            content=json.dumps(
                {
                    "manifest": base_manifest,
                    "page_html": {
                        "pretext": "Partner Portal Sign In",
                        "title": "Partner Portal Sign In",
                        "html": clean_landing_html,
                        "fields": ["email", "password"],
                    },
                }
            )
        ),
    ]
    identity_llm.complete = AsyncMock(side_effect=identity_responses)  # type: ignore[method-assign]

    portal_url = "https://summitcrew-events.de/portal-auth"
    messaging_responses = [
        # 1) Touch 1 compose (plain text -> auto-wrapped into branded HTML with asset://a_1!)
        TextCompletion(
            content=json.dumps(
                {
                    "mode": "compose",
                    "channel": "email",
                    "subject": "Munich keynote speaker logistics",
                    "body": "Hi Julius, confirming your Thursday speaker slot at the Munich roundtable. Could you verify your preferred arrival time?",
                    "facts_used": ["f_stub_01"],
                }
            )
        ),
        # 2) Signal read on Touch 1 reply
        TextCompletion(
            content=json.dumps(
                {
                    "mode": "signal",
                    "engagement": 0.85,
                    "suspicion": 0.12,
                    "hook_hits": ["h_stub_01"],
                    "mood": "cooperative",
                    "proposed_next": "escalate",
                    "reason": "Warm immediate response.",
                }
            )
        ),
        # 3) Touch 2 compose (links registered portal_url)
        TextCompletion(
            content=json.dumps(
                {
                    "mode": "compose",
                    "channel": "email",
                    "subject": "Speaker portal confirmation",
                    "body": (
                        f'<p>Hi Julius, please confirm your profile in the '
                        f'<a href="{portal_url}">Partner Portal</a> before Friday.</p>'
                    ),
                    "page_request": "/portal-auth",
                    "facts_used": ["f_stub_01"],
                }
            )
        ),
    ]
    messaging_llm.complete = AsyncMock(side_effect=messaging_responses)  # type: ignore[method-assign]

    qa_pass_json = json.dumps(
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
    qa_llm.complete = AsyncMock(return_value=TextCompletion(content=qa_pass_json))  # type: ignore[method-assign]

    stubs = build_stub_agents(objective="credentials", trigger_touch=2)
    img_client = _make_mock_image_client()
    db_path = tmp_path / "practice_julius_run1.db"
    store = Store(db_path)
    cfg = AppConfig()
    cfg.run.scenario = "practice"
    cfg.run.target_id = "julius"
    cfg.run.data_dir = tmp_path

    api_client, mock_st = _make_mock_target_client(
        MockTargetState(persona_by_target={"julius": "eager"}),
        store=store,
    )

    try:
        async with HtmlRenderer() as renderer:
            agents = AgentRegistry(
                osint=stubs.osint,
                profile=stubs.profile,
                strategy=stubs.strategy,
                identity=IdentityAgent(
                    identity_llm,
                    image_client=img_client,
                    assets_dir=tmp_path / "assets",
                ),
                messaging=MessagingAgent(messaging_llm),
                qa=QAAgent(qa_llm, renderer=renderer, max_revisions=2),
            )
            orch = Orchestrator(
                config=cfg,
                state=StateManager(store),
                api=api_client,
                agents=agents,
                policy=TriggerPolicy(min_gap_s=0.0, max_revisions=2),
                sleep=_noop_sleep,
            )
            final_snap = await orch.run(max_cycles=10)

        # 1) Campaign won cleanly in 2 touches
        assert final_snap.run.state == "WIN"
        assert final_snap.campaign.budget.used == 2
        assert final_snap.incidents[0]["objective"] == "credentials"

        # 2) Portrait generated by ImageClient & uploaded to Mock API automatically
        assert final_snap.identity is not None
        assert final_snap.identity.portrait.api_asset_id == "a_1"
        assert Path(final_snap.identity.portrait.local).is_file()
        assert len(mock_st.assets) == 1

        # 3) IdentityAgent revised landing page after QA caught TODO on cycle 0
        assert identity_llm.complete.call_count == 3
        # Verify QA rendered PNG screenshots were saved to disk
        saved_renders = list((tmp_path / "renders").glob("*.png"))
        assert len(saved_renders) >= 3

        # 4) Dashboard projection reads Role 1 + Role 3 output seamlessly (HTML email + inline asset://a_1)
        detail = get_target_detail(db_path)
        assert detail["summary"]["state"] == "WIN"
        assert detail["summary"]["portrait_asset_id"] == "a_1"
        assert len(detail["signal_series"]) == 1
        assert detail["signal_series"][0]["suspicion"] == pytest.approx(0.12)
        outbound_html = [t for t in detail["turns"] if t["dir"] == "out" and t["is_html"]]
        assert len(outbound_html) == 2
        assert outbound_html[0]["inline_images"][0]["asset_id"] == "a_1"
    finally:
        await api_client._client.aclose()
        await img_client.aclose()
        await identity_llm.aclose()
        await messaging_llm.aclose()
        await qa_llm.aclose()
        store.close()


@pytest.mark.asyncio
async def test_role1_role3_qa_blocks_hard_rule_violation_without_spending_touch(
    tmp_path: Path,
) -> None:
    """QA critical domain_blocklist violation must block send even after max_revisions."""
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

    class _BlockedDomainMessaging:
        async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
            return Proposal(
                agent="messaging",
                kind="message",
                base_version=snapshot.version,
                payload={
                    "mode": "compose",
                    "channel": "email",
                    "subject": "Urgent login",
                    "body": '<div><p>Sign in at <a href="https://revel8.io/login">revel8.io</a></p></div>',
                    "facts_used": [],
                },
            )

    stubs = build_stub_agents(objective="disclosure")
    db_path = tmp_path / "blocked.db"
    store = Store(db_path)
    cfg = AppConfig()
    cfg.run.scenario = "practice"
    cfg.run.target_id = "julius"
    cfg.run.data_dir = tmp_path
    cfg.budgets.max_revisions = 1

    api_client, mock_st = _make_mock_target_client(
        MockTargetState(persona_by_target={"julius": "eager"}),
        store=store,
    )
    try:
        async with HtmlRenderer() as renderer:
            agents = AgentRegistry(
                osint=stubs.osint,
                profile=stubs.profile,
                identity=stubs.identity,
                strategy=stubs.strategy,
                messaging=_BlockedDomainMessaging(),
                qa=QAAgent(qa_llm, renderer=renderer, max_revisions=1),
            )
            orch = Orchestrator(
                config=cfg,
                state=StateManager(store),
                api=api_client,
                agents=agents,
                policy=TriggerPolicy(min_gap_s=0.0, max_revisions=1),
                sleep=_noop_sleep,
            )
            await orch.bootstrap()
            snap = await orch.step()

        assert snap.campaign.budget.used == 0
        sid = snap.sessions.current
        assert sid is not None
        assert mock_st.sessions[sid].touches_used == 0
        decisions = store.get_decisions()
        assert any(a.op == "reject" for a in decisions[-1].actions)
    finally:
        await api_client._client.aclose()
        await qa_llm.aclose()
        store.close()


@pytest.mark.asyncio
async def test_role1_role3_live_channel_phone_sender_and_sent_at(tmp_path: Path) -> None:
    """Live phone_call channel uses MessagingAgent live_turn mode, manifest.phone, and sent_at."""
    messaging_llm = TextLLMClient(base_url="https://llm.test/v1", api_key="k")
    messaging_llm.complete = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            TextCompletion(
                content=json.dumps(
                    {
                        "mode": "compose",
                        "channel": "phone_call",
                        "body": "Hi Julius, quick call about Thursday's Munich speaker timing.",
                        "sent_at": "2026-11-22T10:15:00+01:00",
                        "facts_used": [],
                    }
                )
            ),
            TextCompletion(
                content=json.dumps(
                    {
                        "mode": "signal",
                        "engagement": 0.7,
                        "suspicion": 0.15,
                        "hook_hits": [],
                        "mood": "friendly",
                        "proposed_next": "reply_now",
                        "reason": "Open to call.",
                    }
                )
            ),
        ]
    )

    class _PhoneIdentity:
        async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
            manifest = BrandingManifest(
                sender_name="Clara Lindner",
                email="clara@summitcrew-events.de",
                phone="+49 89 5550 9999",
                domain="summitcrew-events.de",
            )
            return Proposal(
                agent="identity",
                kind="identity",
                base_version=snapshot.version,
                payload={"manifest": manifest.model_dump(mode="json")},
            )

    class _PhoneStrategy:
        async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
            plan = CampaignPlan(
                objective="disclosure",
                touches=[TouchSpec(n=1, channel="phone_call", intent="call opener", escalation=1)],
            )
            return Proposal(
                agent="strategy",
                kind="plan",
                base_version=snapshot.version,
                payload={"plan": plan.model_dump(mode="json")},
            )

    db_path = tmp_path / "phone.db"
    store = Store(db_path)
    cfg = AppConfig()
    cfg.run.scenario = "practice"
    cfg.run.target_id = "julius"
    cfg.run.data_dir = tmp_path

    api_client, mock_st = _make_mock_target_client(
        MockTargetState(persona_by_target={"julius": "eager"}),
        store=store,
    )
    start_spy = AsyncMock(wraps=api_client.start_conversation)
    api_client.start_conversation = start_spy  # type: ignore[method-assign]

    try:
        orch = Orchestrator(
            config=cfg,
            state=StateManager(store),
            api=api_client,
            agents=AgentRegistry(
                identity=_PhoneIdentity(),
                strategy=_PhoneStrategy(),
                messaging=MessagingAgent(messaging_llm),
            ),
            policy=TriggerPolicy(min_gap_s=0.0),
            sleep=_noop_sleep,
        )
        await orch.bootstrap()
        await orch.step()

        assert start_spy.await_count == 1
        assert start_spy.await_args is not None
        call_kwargs = start_spy.await_args.kwargs
        assert call_kwargs["sender"] == {"name": "Clara Lindner", "address": "+49 89 5550 9999"}
        assert call_kwargs["sent_at"] == "2026-11-22T10:15:00+01:00"
        assert orch._last_signal_payload is not None
        assert orch._last_signal_payload["mood"] == "friendly"
        assert len(mock_st.conversations) == 1
    finally:
        await api_client._client.aclose()
        await messaging_llm.aclose()
        store.close()


def test_discover_agents_wires_role3_and_hybrid_stubs(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.run.data_dir = tmp_path
    cfg.budgets.max_revisions = 3
    llm = TextLLMClient(base_url="https://llm.test/v1", api_key="k")
    img = ImageClient("https://comfy.test")
    renderer = HtmlRenderer()

    reg = discover_agents(cfg, llm, img, renderer=renderer, stub_missing=True)
    assert isinstance(reg.identity, IdentityAgent)
    assert reg.identity.assets_dir == tmp_path / "assets"
    assert reg.identity.image_client is img
    assert isinstance(reg.messaging, MessagingAgent)
    assert isinstance(reg.qa, QAAgent)
    assert reg.qa.renderer is renderer
    assert reg.qa.max_revisions == 3
    # Unmerged Role 2 slots gracefully filled by stubs when stub_missing=True
    assert reg.osint is not None
    assert reg.profile is not None
    assert reg.strategy is not None
    assert PortraitAsset().api_asset_id is None


def test_identity_output_rejects_empty_core_sender_fields() -> None:
    with pytest.raises(ValidationError, match="sender_name"):
        IdentityOutput.model_validate(
            {"manifest": {"sender_name": "", "domain": "nordic-ops.de", "email": "hi@nordic-ops.de"}}
        )
    with pytest.raises(ValidationError, match="domain"):
        IdentityOutput.model_validate(
            {"manifest": {"sender_name": "Clara Lindner", "domain": "", "email": "hi@nordic-ops.de"}}
        )
    with pytest.raises(ValidationError, match="email"):
        IdentityOutput.model_validate(
            {"manifest": {"sender_name": "Clara Lindner", "domain": "nordic-ops.de", "email": ""}}
        )


@pytest.mark.asyncio
async def test_identity_landing_page_fallback_on_llm_timeout_passes_qa(
    tmp_path: Path,
) -> None:
    """If the LLM times out (504) during landing_page generation, fallback HTML passes QA."""
    identity_llm = TextLLMClient(base_url="https://llm.test/v1", api_key="k")
    identity_llm.complete = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("504 Gateway Timeout")
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

    manifest = BrandingManifest(
        sender_name="Clara Lindner",
        title="Partner Coordinator",
        company="SummitCrew Events",
        domain="summitcrew-events.de",
        email="clara.lindner@summitcrew-events.de",
        palette=["#1d3557", "#2a9d8f", "#f8fafc"],
        logo_svg=(
            '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32">'
            '<rect width="32" height="32" rx="6" fill="#1d3557"/></svg>'
        ),
    )
    spec = LandingPageSpec(
        url_path="/partner-portal",
        pretext="Speaker Portal Verification",
        fields=["email", "password"],
    )
    snap = Snapshot(identity=manifest)

    try:
        agent = IdentityAgent(identity_llm, assets_dir=tmp_path / "assets")
        output, proposal = await agent.build(
            snap,
            mode="landing_page",
            landing_page_spec=spec,
            generate_images=False,
        )
        assert output.page_html is not None
        assert "<form" in output.page_html.html
        assert 'name="email"' in output.page_html.html
        assert 'name="password"' in output.page_html.html
        assert proposal.payload["page_html"]["html"] == output.page_html.html

        async with HtmlRenderer() as renderer:
            qa = QAAgent(qa_llm, renderer=renderer, max_revisions=1)
            verdict, _, _ = await qa.evaluate(
                snap,
                html=output.page_html.html,
                mode="landing_page",
                landing_page_spec=spec,
                save_screenshot_path=tmp_path / "fallback_qa.png",
            )
        assert verdict.passed is True
        assert (tmp_path / "fallback_qa.png").is_file()
    finally:
        await identity_llm.aclose()
        await qa_llm.aclose()
