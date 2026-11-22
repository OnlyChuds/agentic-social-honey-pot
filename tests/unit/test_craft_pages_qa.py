from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.agents.identity import wrap_branded_email_html, wrap_email_html
from src.agents.landing_pages import render_landing_page, select_harvest_type
from src.agents.qa import check_past_dates, check_placeholders, run_deterministic_checks
from src.agents.visual_artifacts import build_visual_artifact_html, render_visual_artifact_png
from src.models import BrandingManifest, LandingPageSpec
from src.render.screenshot import HtmlRenderer


def _manifest() -> BrandingManifest:
    return BrandingManifest(
        concept="DACH Summit Program Coordinator",
        sender_name="Clara Brandt",
        title="Program Coordinator",
        company="DACH Cloud Forum GmbH",
        real_org="Cloud Forum",
        domain="dachforum-events.de",
        email="clara.brandt@dachforum-events.de",
        palette=["#1e293b", "#2563eb"],
        fonts=["Inter", "system-ui", "sans-serif"],
        signature="Clara Brandt\nProgram Coordinator\nDACH Cloud Forum GmbH",
        footer_legal="DACH Cloud Forum GmbH · Munich",
    )


def test_wrap_email_html_three_styles() -> None:
    manifest = _manifest()
    body = "Hi Alex,\n\nQuick follow-up on the agenda at https://dachforum-events.de/agenda.\n\nBest,\nClara"

    personal = wrap_email_html(manifest, subject="Re: Panel timing", body_html=body, style="personal_html")
    assert "<!doctype html>" in personal.lower()
    assert "background:#ffffff" in personal
    # Personal HTML must NOT render the dark corporate header card or gray backdrop
    assert "background:#1e293b" not in personal
    assert "background:#f3f4f6" not in personal
    assert 'href="https://dachforum-events.de/agenda"' in personal
    assert "Clara Brandt" in personal

    plain = wrap_email_html(manifest, subject="Re: Panel timing", body_html=body, style="plain_text")
    assert "<!doctype html>" not in plain.lower()
    assert "<p" not in plain.lower()
    assert "https://dachforum-events.de/agenda" in plain
    assert "Clara Brandt" in plain

    branded = wrap_email_html(manifest, subject="Portal notice", body_html=body, style="branded_card")
    branded_alias = wrap_branded_email_html(manifest, subject="Portal notice", body_html=body)
    assert branded == branded_alias
    assert "background:#1e293b" in branded
    assert "DACH Cloud Forum GmbH · Munich" in branded


def test_disclosure_intake_and_event_overview_selection_and_rendering() -> None:
    manifest = _manifest()

    intake_spec = LandingPageSpec(
        url_path="/onboarding-intake",
        pretext="Partner technical architecture questionnaire",
        fields=["email", "architecture_notes", "internal_contact"],
    )
    assert select_harvest_type(intake_spec) == "disclosure_intake"
    intake_art = render_landing_page(intake_spec, manifest)
    assert 'name="architecture_notes"' in intake_art.html
    assert 'name="internal_contact"' in intake_art.html
    assert "Partner Technical Onboarding" in intake_art.html

    overview_spec = LandingPageSpec(
        url_path="/summit-briefing",
        pretext="Executive summit agenda overview",
        fields=["full_name", "email", "company"],
    )
    assert select_harvest_type(overview_spec) == "event_overview"
    overview_art = render_landing_page(overview_spec, manifest)
    assert 'name="full_name"' in overview_art.html
    assert "Executive Roundtable Brief" in overview_art.html


def test_qa_catches_speaker_panelist_placeholders_and_past_dates() -> None:
    placeholder_issues = check_placeholders(
        "Panel lineup: Speaker 1, Panelist B, Presenter 2 from Company X."
    )
    fixes = " ".join(issue.fix for issue in placeholder_issues)
    assert "Speaker 1" in fixes
    assert "Panelist B" in fixes
    assert "Presenter 2" in fixes
    assert "Company X" in fixes

    ref = date(2026, 11, 20)
    past_issues = check_past_dates(
        "Join us 12-13 Nov in Munich, or confirm by November 15, 2026 (deadline 2026-11-10).",
        reference_date=ref,
    )
    assert len(past_issues) == 3
    assert all(i.criterion == "detail_precision" and i.severity == "major" for i in past_issues)

    # Upcoming dates and next-January rollover must NOT trigger past-date warnings
    future_issues = check_past_dates(
        "Join us 25-26 Nov, December 4, 2026, 2026-12-10, or 15 Jan.",
        reference_date=ref,
    )
    assert future_issues == []

    # Deterministic checks run check_past_dates when reference_date is passed
    det_issues = run_deterministic_checks(
        html="<p>Summit on 12-13 Nov with Clara Brandt at DACH Cloud Forum GmbH.</p>",
        manifest=_manifest(),
        reference_date=ref,
    )
    assert any("12-13 Nov" in i.fix for i in det_issues)


@pytest.mark.asyncio
async def test_render_visual_artifact_png(tmp_path: Path) -> None:
    manifest = _manifest()
    html = build_visual_artifact_html(
        title="DACH Cloud Forum — Executive Roundtable",
        subtitle="Confirmed Agenda & Speaker Briefing · Munich",
        bullets=[
            "09:30 CET — Opening Keynote: Sovereign AI & Enterprise Resilience",
            "11:00 CET — Closed-Door CISO & Founder Roundtable",
            "12:30 CET — Private Partner Lunch & Press Q&A",
        ],
        manifest=manifest,
    )
    assert "DACH Cloud Forum" in html
    assert "Closed-Door CISO" in html

    out_path = tmp_path / "agenda_brief.png"
    async with HtmlRenderer() as renderer:
        saved = await render_visual_artifact_png(
            renderer,
            title="DACH Cloud Forum — Executive Roundtable",
            subtitle="Confirmed Agenda & Speaker Briefing · Munich",
            bullets=[
                "09:30 CET — Opening Keynote: Sovereign AI & Enterprise Resilience",
                "11:00 CET — Closed-Door CISO & Founder Roundtable",
            ],
            manifest=manifest,
            out_path=out_path,
        )
    assert saved == out_path
    assert out_path.is_file()
    raw = out_path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(raw) > 2000
