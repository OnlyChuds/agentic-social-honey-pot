"""IdentityAgent wiring for the offensive lookalike generator + harvest templates."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from src.agents.identity import IdentityAgent
from src.agents.qa import check_domain_blocklist
from src.llm.text import TextCompletion, TextLLMClient
from src.models import (
    BrandingManifest,
    LandingPageSpec,
    PortraitAsset,
    Snapshot,
    TargetInfo,
    VoiceStyle,
)

LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="28" height="28" viewBox="0 0 28 28">'
    '<rect width="28" height="28" rx="6" fill="#3b82f6"/>'
    "</svg>"
)


def _manifest(**overrides: str) -> BrandingManifest:
    base: dict[str, str] = {
        "concept": "Portal operations partner",
        "sender_name": "Clara Brandt",
        "title": "Operations Coordinator",
        "company": "NordCloud Ops GmbH",
        "real_org": "NordCloud",
        "domain": "nordcloud-ops-portal.de",
        "email": "clara.brandt@nordcloud-ops-portal.de",
    }
    base.update(overrides)
    return BrandingManifest(
        **base,
        portrait=PortraitAsset(local="", api_asset_id="a_1"),
        logo_svg=LOGO_SVG,
        palette=["#1e293b", "#3b82f6"],
        fonts=["Inter", "system-ui", "sans-serif"],
        signature="Clara Brandt\nOperations Coordinator\nNordCloud Ops GmbH",
        footer_legal="NordCloud Ops GmbH · Berlin",
        voice=VoiceStyle(tone="concise professional", emoji="none", length="2-4 sentences"),
    )


def _snapshot(manifest: BrandingManifest | None = None) -> Snapshot:
    return Snapshot(
        version=1,
        target=TargetInfo(id="t_1", name="Alex Weber", role="VP Engineering", org="Acme"),
        identity=manifest,
    )


def _agent() -> tuple[IdentityAgent, TextLLMClient]:
    llm = TextLLMClient(base_url="http://127.0.0.1:9999/v1", api_key="test", model="test")
    agent = IdentityAgent(llm)
    return agent, llm


SPEC = LandingPageSpec(
    pretext="Annual password rotation",
    fields=["email", "password"],
    url_path="/portal",
)


@pytest.mark.asyncio
async def test_landing_page_renders_branded_template_directly_without_llm() -> None:
    agent, llm = _agent()
    try:
        llm.complete = AsyncMock(side_effect=AssertionError("LLM must not be called on landing_page"))  # type: ignore[method-assign]
        manifest = _manifest()
        output, _ = await agent.build(
            _snapshot(manifest),
            mode="landing_page",
            landing_page_spec=SPEC,
            generate_images=False,
        )
        assert llm.complete.call_count == 0
        assert output.page_html is not None
        assert output.page_html.fields == ["email", "password"]
        # Harvest template signature: notice box, no generic "Continue" boilerplate
        assert 'class="notice"' in output.page_html.html
        assert "annual rotation" in output.page_html.html
    finally:
        await llm.aclose()


@pytest.mark.asyncio
async def test_revise_empty_llm_html_falls_back_to_harvest_template() -> None:
    agent, llm = _agent()
    try:
        manifest = _manifest()
        llm.complete = AsyncMock(  # type: ignore[method-assign]
            return_value=TextCompletion(
                content=json.dumps(
                    {
                        "manifest": manifest.model_dump(mode="json"),
                        "image_prompts": [],
                        "page_html": {"pretext": SPEC.pretext, "title": "", "html": "", "fields": []},
                    }
                )
            )
        )
        output, _ = await agent.build(
            _snapshot(manifest),
            mode="revise",
            landing_page_spec=SPEC,
            generate_images=False,
        )
        assert llm.complete.call_count == 1
        assert output.page_html is not None
        assert "password" in output.page_html.fields
        assert 'class="notice"' in output.page_html.html
    finally:
        await llm.aclose()


@pytest.mark.asyncio
async def test_blocked_domain_is_repaired_by_lookalike_generator() -> None:
    agent, llm = _agent()
    try:
        llm.complete = AsyncMock(  # type: ignore[method-assign]
            return_value=TextCompletion(
                content=json.dumps(
                    {
                        "manifest": _manifest(
                            domain="revel8.com",
                            email="clara.brandt@revel8.com",
                        ).model_dump(mode="json"),
                        "image_prompts": [],
                    }
                )
            )
        )
        output, _ = await agent.build(
            _snapshot(_manifest()),
            mode="bootstrap",
            extra_blocked_domains=("revel8.com",),
            generate_images=False,
        )
        assert output.manifest.domain != "revel8.com"
        assert output.manifest.domain.endswith(".com")
        assert output.manifest.email.endswith(f"@{output.manifest.domain}")
        assert output.manifest.email.lower().startswith("clara.brandt@")
        assert check_domain_blocklist(
            [output.manifest.domain, output.manifest.email],
            extra_blocked=["revel8.com"],
        ) == []
    finally:
        await llm.aclose()


@pytest.mark.asyncio
async def test_subdomain_of_real_root_is_repaired() -> None:
    agent, llm = _agent()
    try:
        llm.complete = AsyncMock(  # type: ignore[method-assign]
            return_value=TextCompletion(
                content=json.dumps(
                    {
                        "manifest": _manifest(
                            domain="login.allianz.com",
                            email="clara.brandt@login.allianz.com",
                        ).model_dump(mode="json"),
                        "image_prompts": [],
                    }
                )
            )
        )
        output, _ = await agent.build(
            _snapshot(_manifest()),
            mode="bootstrap",
            generate_images=False,
        )
        assert "allianz.com" not in output.manifest.domain
        assert check_domain_blocklist(
            [output.manifest.domain, output.manifest.email],
        ) == []
    finally:
        await llm.aclose()
