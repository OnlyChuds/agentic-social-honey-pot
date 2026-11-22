from __future__ import annotations

from typing import get_args

import pytest

from src.agents import domains
from src.agents.identity import PageArtifactSpec as IdentityPageArtifactSpec
from src.agents.landing_pages import (
    MAX_LANDING_PAGE_FIELDS,
    TEMPLATE_COPY,
    HarvestType,
    PageArtifactSpec,
    render_landing_page,
    select_harvest_type,
)
from src.agents.qa import (
    QAAgent,
    check_domain_blocklist,
    run_deterministic_checks,
)
from src.llm.text import TextLLMClient
from src.models import BrandingManifest, LandingPageSpec, Snapshot, TargetInfo
from src.render.screenshot import HtmlRenderer

SAMPLE_LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">'
    '<rect width="24" height="24" rx="5" fill="#3b82f6"/>'
    '<circle cx="12" cy="12" r="5" fill="#ffffff"/>'
    "</svg>"
)


def _sample_manifest(*, company: str = "NordCloud & Partners GmbH", logo_svg: str = SAMPLE_LOGO_SVG) -> BrandingManifest:
    return BrandingManifest(
        concept="Cloud operations partner",
        sender_name="Clara Brandt",
        title="Operations Coordinator",
        company=company,
        real_org="NordCloud",
        domain="nordcloud-ops-portal.de",
        email="clara.brandt@nordcloud-ops-portal.de",
        logo_svg=logo_svg,
        palette=["#1e293b", "#2563eb"],
        fonts=["Inter", "system-ui", "sans-serif"],
        signature="Clara Brandt\nOperations Coordinator\nNordCloud & Partners GmbH",
        footer_legal=f"{company} · Berlin · Automated portal notice",
    )


def test_page_artifact_spec_shared_and_harvest_types_complete() -> None:
    assert IdentityPageArtifactSpec is PageArtifactSpec
    harvest_types = set(get_args(HarvestType))
    assert harvest_types == set(TEMPLATE_COPY.keys())
    assert len(harvest_types) == 8


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (LandingPageSpec(pretext="Partner portal login", fields=["email", "password"]), "portal_login"),
        (LandingPageSpec(pretext="Annual password rotation", fields=["email", "password"]), "password_reset"),
        (LandingPageSpec(pretext="Verify device MFA", fields=["email", "one_time_code"]), "mfa_reauth"),
        (LandingPageSpec(pretext="Signed contract vault", fields=["email", "passcode"]), "document_vault"),
        (LandingPageSpec(pretext="Invoice billing update", fields=["email", "billing_reference"]), "invoice_update"),
        (LandingPageSpec(pretext="Confirm panel slot", fields=["email", "preferred_slot"]), "scheduling"),
        # Regression: 'nda' must not substring-match 'calendar' or 'standard'
        (LandingPageSpec(pretext="Confirm calendar meeting", fields=["email"]), "scheduling"),
        (LandingPageSpec(pretext="Standard portal login", fields=["email"]), "portal_login"),
        # Field signature takes precedence over pretext keywords
        (LandingPageSpec(pretext="Portal access", fields=["email", "otp"]), "mfa_reauth"),
        # Regression: ["email", "password"] must never pick mfa_reauth (or non-password templates)
        (LandingPageSpec(pretext="Partner Portal Verification", fields=["email", "password"]), "portal_login"),
        (LandingPageSpec(pretext="Verify device MFA", fields=["email", "password"]), "portal_login"),
        (LandingPageSpec(pretext="Speaker Portal Verification", fields=["email", "password"]), "portal_login"),
        (LandingPageSpec(pretext="Account verification", fields=["email", "password"]), "portal_login"),
        (LandingPageSpec(pretext="Password verification reset", fields=["email", "password"]), "password_reset"),
        (LandingPageSpec(pretext="Signed document verification", fields=["email", "password"]), "document_vault"),
    ],
)
def test_select_harvest_type(spec: LandingPageSpec, expected: HarvestType) -> None:
    assert select_harvest_type(spec) == expected


def test_render_landing_page_field_normalization_and_escaping() -> None:
    manifest = _sample_manifest(company="Acme & Sons <EU>", logo_svg="")
    raw_fields = [" email ", "Email", "password", ""] + [f"extra_{i}" for i in range(25)]
    spec = LandingPageSpec(pretext="Partner login", fields=raw_fields)

    artifact = render_landing_page(spec, manifest)
    assert len(artifact.fields) == MAX_LANDING_PAGE_FIELDS
    assert artifact.fields[:2] == ["email", "password"]
    # Title stored in PageArtifactSpec should be plain text (not double-escaped)
    assert "Acme & Sons <EU>" in artifact.title
    assert "&amp;amp;" not in artifact.html
    # No empty logo wrapper when logo_svg is omitted
    assert '<div class="head"><span></span>' not in artifact.html


@pytest.mark.asyncio
async def test_all_templates_pass_playwright_and_qa_gate() -> None:
    manifest = _sample_manifest()
    snapshot = Snapshot(
        version=1,
        target=TargetInfo(id="t_1", name="Alex Weber", role="VP Engineering", org="Acme"),
        identity=manifest,
    )
    llm = TextLLMClient(base_url="http://127.0.0.1:9999/v1", api_key="test", model="test")
    cases: list[tuple[HarvestType, LandingPageSpec]] = [
        ("portal_login", LandingPageSpec(pretext="Partner portal login", fields=["email", "password"])),
        ("password_reset", LandingPageSpec(pretext="Annual password rotation", fields=["email", "password"])),
        ("mfa_reauth", LandingPageSpec(pretext="Verify device MFA", fields=["email", "one_time_code"])),
        ("document_vault", LandingPageSpec(pretext="Signed contract vault", fields=["email", "passcode"])),
        (
            "invoice_update",
            LandingPageSpec(
                pretext="Invoice billing update",
                fields=["email", "billing_reference", "card_last4"],
            ),
        ),
        ("scheduling", LandingPageSpec(pretext="Confirm panel slot", fields=["email", "preferred_slot"])),
        (
            "disclosure_intake",
            LandingPageSpec(
                pretext="Partner technical onboarding questionnaire",
                fields=["email", "architecture_notes", "internal_contact"],
            ),
        ),
        (
            "event_overview",
            LandingPageSpec(
                pretext="Executive summit overview and RSVP",
                fields=["full_name", "email", "company"],
            ),
        ),
    ]

    try:
        async with HtmlRenderer() as renderer:
            qa_agent = QAAgent(llm, renderer=renderer)
            for expected_type, spec in cases:
                assert select_harvest_type(spec) == expected_type
                artifact = render_landing_page(spec, manifest)
                page_url = f"https://{manifest.domain}{spec.url_path}"

                verdict, proposal, render_res = await qa_agent.evaluate(
                    snapshot,
                    html=artifact.html,
                    mode="landing_page",
                    channel="landing_page",
                    page_url=page_url,
                    landing_page_spec=spec,
                    render=True,
                    run_llm=False,
                )

                assert verdict.passed, f"{expected_type} failed QA: {verdict.issues}"
                assert verdict.issues == [], f"{expected_type} had QA issues: {verdict.issues}"
                assert proposal.kind == "qa_verdict"
                assert render_res is not None
                assert not render_res.has_horizontal_overflow
                assert render_res.broken_images == []
                assert render_res.external_requests == []
                assert "&amp;" not in render_res.visible_text
                assert manifest.company in render_res.visible_text
                rendered_names = [f.name for f in render_res.form_fields]
                for declared in artifact.fields:
                    assert declared in rendered_names
    finally:
        await llm.aclose()


def test_deterministic_checks_catch_blocked_page_url() -> None:
    manifest = _sample_manifest()
    spec = LandingPageSpec(pretext="Portal login", fields=["email", "password"])
    artifact = render_landing_page(spec, manifest)
    issues = run_deterministic_checks(
        html=artifact.html,
        manifest=manifest,
        page_url="https://login.revel8.com/portal",
        landing_page_spec=spec,
    )
    assert any(i.criterion == "domain_blocklist" and i.severity == "critical" for i in issues)


def test_domains_module_passes_qa_blocklist() -> None:
    for root in ("revel8.com", "https://www.acme.co.uk/login", "stackit.cloud", "allianz.com"):
        candidates = domains.generate_lookalike_domains(root, limit=15)
        assert candidates
        assert check_domain_blocklist(candidates, extra_blocked=[root]) == []

    safe = domains.ensure_lookalike_domain("login.revel8.com", "revel8.com")
    assert safe != "login.revel8.com"
    assert check_domain_blocklist([safe], extra_blocked=["revel8.com"]) == []

    assert (
        domains.derive_sender_email("Dr. Jürgen Müller", "revel8-portal.com")
        == "juergen.mueller@revel8-portal.com"
    )
