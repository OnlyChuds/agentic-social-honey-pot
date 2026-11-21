from __future__ import annotations

import argparse
import hashlib
import struct
import time
import zlib
from pathlib import Path
from typing import Any

from src.models import (
    BrandingManifest,
    CampaignPlan,
    DecisionAction,
    DecisionRecord,
    Fact,
    Hook,
    HookEngagement,
    LandingPageSpec,
    PortraitAsset,
    Profile,
    Proposal,
    PsychTrait,
    SessionRestartSpec,
    Stressor,
    TargetInfo,
    TouchSpec,
    VoiceStyle,
)
from src.orchestrator.state import StateManager
from src.store.db import Store


def _reset_db(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}")
        if candidate.exists():
            candidate.unlink()


def _act(op: str, **extra: Any) -> DecisionAction:
    return DecisionAction.model_validate({"op": op, **extra})


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack("!I", len(data)) + chunk_type + data + struct.pack("!I", crc)


def _write_demo_png(
    path: Path,
    *,
    width: int,
    height: int,
    bg_rgb: tuple[int, int, int],
    accent_rgb: tuple[int, int, int],
) -> tuple[bytes, str]:
    """Generate a valid minimal RGB PNG with a subtle geometric accent (no external image libs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = bytearray()
    cx, cy = width // 2, height // 2
    radius_sq = (min(width, height) // 3) ** 2
    for y in range(height):
        rows.append(0)  # PNG filter type 0
        for x in range(width):
            dist_sq = (x - cx) ** 2 + (y - cy) ** 2
            if dist_sq <= radius_sq or y < max(4, height // 14):
                rows.extend(accent_rgb)
            else:
                rows.extend(bg_rgb)
    ihdr = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), level=6))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(raw_png)
    return raw_png, hashlib.sha256(raw_png).hexdigest()


def _seed_julius(data_dir: Path) -> Path:
    """Seed Julius Muth: BOOT -> RESEARCH -> ENGAGE -> WIN over 4 touches (credentials win)."""
    db_path = data_dir / "practice_julius_run1.db"
    _reset_db(db_path)

    with Store(db_path) as store:
        state = StateManager(store)
        state.configure_run(
            scenario="practice",
            target=TargetInfo(
                id="julius",
                name="Julius Muth",
                role="Co-Founder & Co-CEO (GTM)",
                org="revel8",
            ),
            run_no=1,
            session_max_touches=30,
            sessions_allowed=60,
        )
        state.start_session("sid_julius_01", session_max_touches=30)
        state.transition_state("RESEARCH", reason="session created; starting OSINT and identity")

        facts = [
            Fact(
                fact_id="f_jul_01",
                claim="Julius Muth is Co-Founder & Co-CEO leading GTM and partnerships at revel8 in Munich.",
                category="role",
                source="https://revel8.io/about",
                provenance="osint",
                confidence=0.96,
                osint_grade="A",
            ),
            Fact(
                fact_id="f_jul_02",
                claim="Julius spoke on DACH enterprise human-risk panels at Munich Cyber Week 2026.",
                category="talks",
                source="https://munich-cyberweek.example/speakers/julius-muth",
                provenance="osint",
                confidence=0.92,
                osint_grade="A",
            ),
            Fact(
                fact_id="f_jul_03",
                claim="revel8 recently expanded channel partnerships with mid-market MSPs across DACH.",
                category="company",
                source="https://linkedin.com/company/revel8-security",
                provenance="osint",
                confidence=0.84,
                osint_grade="B",
            ),
            Fact(
                fact_id="f_jul_04",
                claim="Julius posts concise founder updates focused on fast enterprise deal velocity.",
                category="social",
                source="https://linkedin.com/in/juliusmuth",
                provenance="osint",
                confidence=0.80,
                osint_grade="B",
            ),
        ]

        hooks = [
            Hook(
                hook_id="h_jul_01",
                topic="DACH CISO Partner Summit keynote & co-hosted pipeline roundtable",
                evidence=["f_jul_02", "f_jul_03"],
                why_leverage="Directly aligns with GTM visibility and enterprise MSP deal flow.",
                confidence=0.86,
                engagement=HookEngagement(touches_touched=0, positive=0, score=0.0),
                status="candidate",
            ),
            Hook(
                hook_id="h_jul_02",
                topic="Joint MSP security awareness benchmark report co-authorship",
                evidence=["f_jul_03", "f_jul_04"],
                why_leverage="Low-friction GTM co-marketing angle with clear inbound lead value.",
                confidence=0.72,
                engagement=HookEngagement(touches_touched=0, positive=0, score=0.0),
                status="candidate",
            ),
        ]

        assets_dir = data_dir / "_demo_assets"
        portrait_path = assets_dir / "clara_portrait.png"
        portrait_bytes, portrait_sha = _write_demo_png(
            portrait_path,
            width=96,
            height=96,
            bg_rgb=(24, 24, 27),
            accent_rgb=(82, 82, 91),
        )
        store.record_asset(
            asset_id="a_1",
            local_path=portrait_path,
            sha256=portrait_sha,
            content_type="image/png",
            size_bytes=len(portrait_bytes),
        )

        banner_path = assets_dir / "dach_summit_badge.png"
        banner_bytes, banner_sha = _write_demo_png(
            banner_path,
            width=240,
            height=80,
            bg_rgb=(17, 24, 39),
            accent_rgb=(56, 189, 248),
        )
        store.record_asset(
            asset_id="a_2",
            local_path=banner_path,
            sha256=banner_sha,
            content_type="image/png",
            size_bytes=len(banner_bytes),
        )

        manifest = BrandingManifest(
            concept="DACH enterprise summit & partner program director",
            sender_name="Clara Lindner",
            title="Head of Partner Programs",
            company="DACH Cyber Forum GmbH",
            real_org="revel8",
            domain="dachcyberforum-partners.de",
            email="clara.lindner@dachcyberforum-partners.de",
            phone="+49 89 4120 9840",
            bio="Organizes executive CISO roundtables and partner showcases across Munich and Zurich.",
            portrait=PortraitAsset(local=str(portrait_path), api_asset_id="a_1"),
            palette=["#0F172A", "#2563EB", "#F8FAFC"],
            signature="Clara Lindner\nHead of Partner Programs · DACH Cyber Forum",
            footer_legal="DACH Cyber Forum GmbH · Maximilianstr. 35, 80539 Munich",
            voice=VoiceStyle(
                tone="warm executive concise",
                emoji="none",
                length="2-3 short sentences",
            ),
        )

        # Commit OSINT + Identity in parallel batch
        base_v = state.snapshot.version
        state.commit_batch(
            [
                Proposal(
                    agent="osint",
                    kind="dossier",
                    base_version=base_v,
                    payload={
                        "facts": [f.model_dump(mode="json") for f in facts],
                        "hook_seeds": [h.model_dump(mode="json") for h in hooks],
                        "psychoprofile": [],
                        "completeness": "high",
                    },
                    rationale="Grade A/B OSINT dossier on Julius Muth and revel8 GTM motion.",
                ),
                Proposal(
                    agent="identity",
                    kind="identity",
                    base_version=base_v,
                    payload={"manifest": manifest.model_dump(mode="json")},
                    rationale="Executive summit partner persona aligned with Munich cyber ecosystem.",
                ),
            ]
        )

        # Initial OSINT-derived profile synthesis
        initial_profile = Profile(
            style_guide=(
                "OSINT hypothesis: energetic GTM co-founder; prefers crisp 2-3 sentence notes "
                "with immediate commercial or event visibility value."
            ),
            psych=[
                PsychTrait(
                    trait="fast_decision_maker",
                    confidence=0.62,
                    evidence=["f_jul_01", "f_jul_04"],
                ),
                PsychTrait(
                    trait="partner_network_oriented",
                    confidence=0.58,
                    evidence=["f_jul_03"],
                ),
                PsychTrait(
                    trait="public_visibility_driven",
                    confidence=0.55,
                    evidence=["f_jul_02"],
                ),
            ],
            stressors=[
                Stressor(
                    topic="DACH enterprise pipeline acceleration",
                    confidence=0.68,
                    evidence=["f_jul_03"],
                )
            ],
            trust_style="Warm peer introductions and recognizable DACH event organizers.",
            watchouts=[],
            timezone_hint="Europe/Berlin",
            language="en",
            overall_confidence=0.64,
        )
        state.commit_proposal(
            Proposal(
                agent="profile",
                kind="profile_update",
                base_version=state.snapshot.version,
                payload={
                    "profile_update": initial_profile.model_dump(mode="json"),
                    "hook_updates": [
                        {
                            "hook_id": "h_jul_01",
                            "status": "active",
                            "confidence": 0.88,
                        }
                    ],
                },
                rationale="Initial profile synthesized from grade A/B OSINT sources.",
            )
        )

        # Initial campaign plan
        plan = CampaignPlan(
            objective="credentials",
            primary_hook="h_jul_01",
            touches=[
                TouchSpec(
                    n=1,
                    channel="email",
                    intent="Warm invitation to Munich CISO Partner Summit roundtable",
                    hook="h_jul_01",
                    escalation=1,
                ),
                TouchSpec(
                    n=2,
                    channel="linkedin_message",
                    intent="Quick LinkedIn nudge referencing Munich Cyber Week panel",
                    hook="h_jul_01",
                    escalation=2,
                ),
                TouchSpec(
                    n=3,
                    channel="email",
                    intent="Confirm speaker slot & preview attendee list",
                    hook="h_jul_01",
                    escalation=3,
                ),
                TouchSpec(
                    n=4,
                    channel="email",
                    intent="Send Partner Summit Speaker Portal credentials confirmation link",
                    hook="h_jul_01",
                    escalation=4,
                ),
            ],
            landing_page=LandingPageSpec(
                pretext="DACH Cyber Forum — Speaker & Partner Portal",
                fields=["email", "password"],
                trigger_touch=4,
                url_path="/speaker-portal",
            ),
            pivot_triggers=["2 touches no reply", "suspicion>=0.6"],
            give_up="touch 10",
        )
        state.commit_proposal(
            Proposal(
                agent="strategy",
                kind="plan",
                base_version=state.snapshot.version,
                payload={"plan": plan.model_dump(mode="json")},
                rationale="4-touch warm rapport sequence culminating in speaker portal sign-in.",
            )
        )

        state.transition_state("ENGAGE", reason="bootstrap complete; entering campaign loop")
        state.log_decision(
            DecisionRecord(
                trigger="boot",
                snapshot_version=state.snapshot.version,
                actions=[_act("commit", sections=["facts", "hooks", "identity", "profile", "campaign.plan"])],
                notes="Bootstrap complete for Julius Muth; high-confidence GTM hook selected.",
            )
        )

        # Touch 1: Email outreach + warm reply + signal (suspicion 0.18, engagement 0.65)
        state.record_outbound_turn(
            channel="email",
            subject="Munich CISO Partner Roundtable — speaker slot for revel8",
            body=(
                "Hi Julius, loved your human-risk panel at Munich Cyber Week. We're hosting an "
                "invite-only DACH CISO & MSP partner roundtable next month and reserved a founder "
                "spotlight for revel8. Worth sending over the 1-pager?"
            ),
            facts_used=["f_jul_01", "f_jul_02", "f_jul_03"],
        )
        ev_r1 = state.record_target_event(
            {
                "type": "reply",
                "channel": "email",
                "body": "Hey Clara, thanks for reaching out! Definitely interested — please send over the details and who's attending.",
            },
            default_channel="email",
        )
        state.commit_proposal(
            Proposal(
                agent="messaging",
                kind="signals",
                base_version=state.snapshot.version,
                payload={
                    "mode": "signal",
                    "channel": "email",
                    "touch_n": 1,
                    "engagement": 0.65,
                    "suspicion": 0.18,
                    "mood": "receptive",
                    "hook_hits": ["h_jul_01"],
                    "proposed_next": "reply_now",
                    "reason": "Immediate positive reply requesting attendee roster.",
                },
                rationale="Touch 1 reply signal extraction.",
            )
        )
        state.commit_proposal(
            Proposal(
                agent="profile",
                kind="profile_update",
                base_version=state.snapshot.version,
                payload={
                    "profile_update": {
                        "style_guide": (
                            "Observed reply #1: casual 'Hey <first name>', exclamation mark, "
                            "responds rapidly when enterprise attendee value is clear."
                        ),
                        "psych": [
                            {
                                "trait": "fast_decision_maker",
                                "confidence": 0.74,
                                "evidence": ["f_jul_01", "f_jul_04", ev_r1.event_id],
                            },
                            {
                                "trait": "partner_network_oriented",
                                "confidence": 0.72,
                                "evidence": ["f_jul_03", ev_r1.event_id],
                            },
                            {
                                "trait": "public_visibility_driven",
                                "confidence": 0.68,
                                "evidence": ["f_jul_02", ev_r1.event_id],
                            },
                        ],
                        "overall_confidence": 0.76,
                    },
                    "hook_updates": [
                        {
                            "hook_id": "h_jul_01",
                            "confidence": 0.91,
                            "engagement": {"touches_touched": 1, "positive": 1, "score": 0.85},
                        }
                    ],
                },
                rationale="Touch 1 confirmed rapid GTM receptivity and casual founder style.",
            )
        )
        state.log_decision(
            DecisionRecord(
                trigger="target_event",
                snapshot_version=state.snapshot.version,
                actions=[_act("send", channel="email", touch_n=1), _act("trigger", agent="profile")],
                notes="Touch 1/30 via email elicited immediate warm reply; suspicion=0.18.",
            )
        )

        # Touch 2: LinkedIn follow-up + reply + signal (suspicion 0.14, engagement 0.82)
        state.record_outbound_turn(
            channel="linkedin_message",
            body=(
                "Hi Julius — just sent the CISO roundtable roster to your inbox (14 DACH enterprise "
                "security leads + 4 MSP partners confirmed). Let me know if the Thursday 16:00 slot works."
            ),
            facts_used=["f_jul_03"],
        )
        ev_r2 = state.record_target_event(
            {
                "type": "reply",
                "channel": "linkedin_message",
                "body": "Thursday 16:00 is locked in on my side. Awesome lineup — let's get our bio & slides uploaded so we're in the brochure.",
            },
            default_channel="linkedin_message",
        )
        state.commit_proposal(
            Proposal(
                agent="messaging",
                kind="signals",
                base_version=state.snapshot.version,
                payload={
                    "mode": "signal",
                    "channel": "linkedin_message",
                    "touch_n": 2,
                    "engagement": 0.82,
                    "suspicion": 0.14,
                    "mood": "enthusiastic",
                    "hook_hits": ["h_jul_01"],
                    "proposed_next": "reply_now",
                    "reason": "Target proactively offered to upload speaker bio & slides.",
                },
                rationale="Touch 2 reply signal extraction.",
            )
        )
        state.commit_proposal(
            Proposal(
                agent="profile",
                kind="profile_update",
                base_version=state.snapshot.version,
                payload={
                    "profile_update": {
                        "style_guide": (
                            "Observed over 2 replies: ultra-short 1-2 sentence mobile replies, "
                            "action-oriented, proactively moves to logistics once value is confirmed."
                        ),
                        "psych": [
                            {
                                "trait": "fast_decision_maker",
                                "confidence": 0.85,
                                "evidence": ["f_jul_01", ev_r1.event_id, ev_r2.event_id],
                            },
                            {
                                "trait": "partner_network_oriented",
                                "confidence": 0.83,
                                "evidence": ["f_jul_03", ev_r2.event_id],
                            },
                            {
                                "trait": "public_visibility_driven",
                                "confidence": 0.80,
                                "evidence": ["f_jul_02", ev_r2.event_id],
                            },
                            {
                                "trait": "frictionless_logistics_bias",
                                "confidence": 0.76,
                                "evidence": [ev_r2.event_id],
                            },
                        ],
                        "overall_confidence": 0.86,
                    },
                    "hook_updates": [
                        {
                            "hook_id": "h_jul_01",
                            "confidence": 0.95,
                            "engagement": {"touches_touched": 2, "positive": 2, "score": 0.93},
                        }
                    ],
                },
                rationale="Discovered frictionless_logistics_bias; style guide refined to observed tone.",
            )
        )
        state.log_decision(
            DecisionRecord(
                trigger="target_event",
                snapshot_version=state.snapshot.version,
                actions=[_act("send", channel="linkedin_message", touch_n=2)],
                notes="Touch 2/30 via LinkedIn locked in slot; suspicion dropped to 0.14.",
            )
        )

        # Touch 3: Branded HTML email with inline image (asset://a_2) + attachment + reply
        state.record_outbound_turn(
            channel="email",
            subject="Re: Munich CISO Partner Roundtable — speaker slot for revel8",
            body=(
                '<div style="font-family:Inter,system-ui,sans-serif;color:#18181b;line-height:1.5;max-width:560px">'
                '<img src="asset://a_2" alt="DACH Cyber Forum Speaker Badge" '
                'style="width:220px;height:auto;border-radius:8px;margin-bottom:12px" />'
                "<p>Hi Julius,</p>"
                "<p>Perfect — I've attached the Munich Roundtable speaker badge and schedule draft. "
                "I'll generate your speaker portal invite shortly so you can confirm your revel8 "
                "profile blurb and co-branding logo before Friday's print deadline.</p>"
                '<p style="color:#52525b;font-size:12px">Clara Lindner · Head of Partner Programs · '
                "DACH Cyber Forum GmbH</p>"
                "</div>"
            ),
            facts_used=["f_jul_01", "f_jul_02"],
            attachments=["a_2", {"filename": "Munich_Roundtable_Schedule.pdf"}],
        )
        ev_r3 = state.record_target_event(
            {
                "type": "reply",
                "channel": "email",
                "body": "Great, send the link over whenever ready and I'll sign in right away.",
            },
            default_channel="email",
        )
        state.commit_proposal(
            Proposal(
                agent="messaging",
                kind="signals",
                base_version=state.snapshot.version,
                payload={
                    "mode": "signal",
                    "channel": "email",
                    "touch_n": 3,
                    "engagement": 0.91,
                    "suspicion": 0.12,
                    "mood": "cooperative",
                    "hook_hits": ["h_jul_01"],
                    "proposed_next": "deploy_landing_page",
                    "reason": "Explicit verbal readiness to sign in to speaker portal.",
                },
                rationale="Touch 3 reply signal extraction.",
            )
        )
        state.commit_proposal(
            Proposal(
                agent="profile",
                kind="profile_update",
                base_version=state.snapshot.version,
                payload={
                    "profile_update": {
                        "style_guide": (
                            "Confirmed conversational profile: brief, direct mobile replies, "
                            "high responsiveness to tight event print deadlines and clean portals."
                        ),
                        "psych": [
                            {
                                "trait": "fast_decision_maker",
                                "confidence": 0.91,
                                "evidence": ["f_jul_01", ev_r1.event_id, ev_r3.event_id],
                            },
                            {
                                "trait": "partner_network_oriented",
                                "confidence": 0.88,
                                "evidence": ["f_jul_03", ev_r2.event_id],
                            },
                            {
                                "trait": "public_visibility_driven",
                                "confidence": 0.84,
                                "evidence": ["f_jul_02", ev_r2.event_id, ev_r3.event_id],
                            },
                            {
                                "trait": "frictionless_logistics_bias",
                                "confidence": 0.86,
                                "evidence": [ev_r2.event_id, ev_r3.event_id],
                            },
                        ],
                        "overall_confidence": 0.93,
                    }
                },
                rationale="High-confidence profile confirmed across 3 consecutive positive replies.",
            )
        )
        state.log_decision(
            DecisionRecord(
                trigger="target_event",
                snapshot_version=state.snapshot.version,
                actions=[_act("send", channel="email", touch_n=3)],
                notes="Touch 3/30 confirmed readiness for portal link; suspicion=0.12, engagement=0.91.",
            )
        )

        # Touch 4: Register landing page, send portal invite, target opens & submits -> WIN!
        page_url = "https://dachcyberforum-partners.de/speaker-portal"
        store.record_page(
            session_id="sid_julius_01",
            page_id="page_jul_01",
            url=page_url,
            title="DACH Cyber Forum — Speaker & Partner Portal",
            fields=["email", "password"],
        )
        state.record_outbound_turn(
            channel="email",
            subject="Your Speaker Portal Access — DACH Cyber Forum",
            body=(
                f"Hi Julius, here is your direct speaker portal link to approve the revel8 listing "
                f"and upload slides: {page_url} — takes under 60 seconds with your work login."
            ),
            facts_used=["f_jul_01"],
        )
        state.record_target_event(
            {
                "type": "link.opened",
                "channel": "email",
                "url": page_url,
                "page_id": "page_jul_01",
            },
            default_channel="email",
        )
        ev_sub = state.record_target_event(
            {
                "type": "page.submitted",
                "channel": "email",
                "url": page_url,
                "page_id": "page_jul_01",
                "fields_submitted": ["email", "password"],
            },
            default_channel="email",
        )
        state.record_incident(
            objective="credentials",
            evidence_event_ids=[ev_sub.event_id],
            summary="Julius Muth submitted credentials on registered Speaker & Partner Portal.",
            details={"url": page_url, "page_id": "page_jul_01", "fields": ["email", "password"]},
        )
        state.log_decision(
            DecisionRecord(
                trigger="target_event",
                snapshot_version=state.snapshot.version,
                actions=[
                    _act(
                        "register_page",
                        url=page_url,
                        title="DACH Cyber Forum — Speaker & Partner Portal",
                        fields=["email", "password"],
                    ),
                    _act("send", channel="email", touch_n=4),
                    _act(
                        "declare_incident",
                        objective="credentials",
                        evidence=[ev_sub.event_id],
                        summary="Credentials submitted on registered page",
                    ),
                ],
                notes="Objective achieved (credentials) on touch 4/30.",
            )
        )

    return db_path


def _seed_tom(data_dir: Path) -> Path:
    """Seed Tom Müller: Active ENGAGE at touch 3/30, guarded suspicion, evolving watchouts."""
    db_path = data_dir / "practice_tom_run1.db"
    _reset_db(db_path)

    with Store(db_path) as store:
        state = StateManager(store)
        state.configure_run(
            scenario="practice",
            target=TargetInfo(
                id="tom",
                name="Tom Müller",
                role="Co-Founder & Co-CEO (Product)",
                org="revel8",
            ),
            run_no=1,
            session_max_touches=30,
            sessions_allowed=60,
        )
        state.start_session("sid_tom_01", session_max_touches=30)
        state.transition_state("RESEARCH", reason="session created; starting OSINT and identity")

        facts = [
            Fact(
                fact_id="f_tom_01",
                claim="Tom Müller is Co-Founder & Co-CEO overseeing product strategy at revel8.",
                category="role",
                source="https://revel8.io/about",
                provenance="osint",
                confidence=0.95,
                osint_grade="A",
            ),
            Fact(
                fact_id="f_tom_02",
                claim="Tom drives revel8's adaptive AI phishing simulation UX and enterprise admin dashboards.",
                category="projects",
                source="https://revel8.io/product",
                provenance="osint",
                confidence=0.88,
                osint_grade="B",
            ),
            Fact(
                fact_id="f_tom_03",
                claim="Tom frequently engages with UX research and B2B SaaS design-partner discussions.",
                category="social",
                source="https://linkedin.com/in/tommueller-revel8",
                provenance="osint",
                confidence=0.79,
                osint_grade="B",
            ),
        ]

        hooks = [
            Hook(
                hook_id="h_tom_01",
                topic="Enterprise SIEM telemetry benchmark & design-partner feedback loop",
                evidence=["f_tom_01", "f_tom_02"],
                why_leverage="Appeals to product discovery and competitive benchmarking rigor.",
                confidence=0.81,
                status="active",
            ),
            Hook(
                hook_id="h_tom_02",
                topic="EU NIS2 compliance UX teardown study",
                evidence=["f_tom_02", "f_tom_03"],
                why_leverage="Directly relevant to European security SaaS product roadmap.",
                confidence=0.74,
                status="candidate",
            ),
        ]

        manifest = BrandingManifest(
            concept="B2B security UX researcher & enterprise design advisor",
            sender_name="Henrik Baumann",
            title="Principal Product Researcher",
            company="Nordic SecUX Benchmark",
            real_org="revel8",
            domain="secux-benchmark.eu",
            email="henrik.baumann@secux-benchmark.eu",
            bio="Publishes independent usability benchmarks for European security platforms.",
            palette=["#111827", "#0D9488", "#F1F5F9"],
            signature="Henrik Baumann\nPrincipal Product Researcher · SecUX Benchmark",
            voice=VoiceStyle(
                tone="analytical peer-to-peer",
                emoji="none",
                length="3-4 concise sentences",
            ),
        )

        base_v = state.snapshot.version
        state.commit_batch(
            [
                Proposal(
                    agent="osint",
                    kind="dossier",
                    base_version=base_v,
                    payload={
                        "facts": [f.model_dump(mode="json") for f in facts],
                        "hook_seeds": [h.model_dump(mode="json") for h in hooks],
                        "psychoprofile": [],
                        "completeness": "medium",
                    },
                    rationale="Product leadership OSINT dossier for Tom Müller.",
                ),
                Proposal(
                    agent="identity",
                    kind="identity",
                    base_version=base_v,
                    payload={"manifest": manifest.model_dump(mode="json")},
                    rationale="Peer security UX researcher persona suited for a Product Co-CEO.",
                ),
            ]
        )

        initial_profile = Profile(
            style_guide=(
                "OSINT baseline: analytical product leader; dislikes generic sales fluff, "
                "responds best to concrete UX artifacts and specific feature questions."
            ),
            psych=[
                PsychTrait(
                    trait="detail_oriented_product_thinker",
                    confidence=0.66,
                    evidence=["f_tom_01", "f_tom_02"],
                ),
                PsychTrait(
                    trait="security_awareness_heightened",
                    confidence=0.60,
                    evidence=["f_tom_02"],
                ),
            ],
            stressors=[
                Stressor(
                    topic="enterprise product roadmap prioritization",
                    confidence=0.64,
                    evidence=["f_tom_02"],
                ),
                Stressor(
                    topic="generic cold outreach fatigue",
                    confidence=0.58,
                    evidence=["f_tom_03"],
                ),
            ],
            trust_style="Evidence-based peer dialogue with specific domain citations.",
            watchouts=[],
            timezone_hint="Europe/Berlin",
            language="en",
            overall_confidence=0.62,
        )
        state.commit_proposal(
            Proposal(
                agent="profile",
                kind="profile_update",
                base_version=state.snapshot.version,
                payload={"profile_update": initial_profile.model_dump(mode="json")},
                rationale="Initial product-leader profile synthesized.",
            )
        )

        plan = CampaignPlan(
            objective="disclosure",
            primary_hook="h_tom_01",
            touches=[
                TouchSpec(n=1, channel="email", intent="Introduce SecUX benchmark study", hook="h_tom_01", escalation=1),
                TouchSpec(n=2, channel="linkedin_message", intent="Follow up with concrete workflow question", hook="h_tom_01", escalation=2),
                TouchSpec(n=3, channel="email", intent="Share methodology summary without external link", hook="h_tom_01", escalation=2),
                TouchSpec(n=4, channel="email", intent="Ask targeted question on upcoming Q4 integration architecture", hook="h_tom_01", escalation=3),
            ],
            pivot_triggers=["2 touches no reply", "suspicion>=0.6"],
            give_up="touch 12",
        )
        state.commit_proposal(
            Proposal(
                agent="strategy",
                kind="plan",
                base_version=state.snapshot.version,
                payload={"plan": plan.model_dump(mode="json")},
                rationale="Measured peer-research cadence avoiding early links.",
            )
        )
        state.transition_state("ENGAGE", reason="bootstrap complete; entering campaign loop")
        state.log_decision(
            DecisionRecord(
                trigger="boot",
                snapshot_version=state.snapshot.version,
                actions=[_act("commit", sections=["facts", "hooks", "identity", "profile", "campaign.plan"])],
                notes="Bootstrap complete for Tom Müller; cautious engagement plan active.",
            )
        )

        # Touch 1: suspicion 0.22, engagement 0.58
        state.record_outbound_turn(
            channel="email",
            subject="UX benchmark note on adaptive simulation dashboards",
            body=(
                "Hi Tom, we're wrapping up a comparative UX review of European human-risk platforms "
                "and noticed how clean revel8's campaign builder flow is. Would love to verify two "
                "workflow assumptions with you before publishing."
            ),
            facts_used=["f_tom_01", "f_tom_02"],
        )
        ev_t1 = state.record_target_event(
            {
                "type": "reply",
                "channel": "email",
                "body": "Hi Henrik, happy to clarify public details if it's quick. Which two workflows are you comparing?",
            },
            default_channel="email",
        )
        state.commit_proposal(
            Proposal(
                agent="messaging",
                kind="signals",
                base_version=state.snapshot.version,
                payload={
                    "mode": "signal",
                    "channel": "email",
                    "touch_n": 1,
                    "engagement": 0.58,
                    "suspicion": 0.22,
                    "mood": "curious",
                    "hook_hits": ["h_tom_01"],
                    "proposed_next": "reply_now",
                    "reason": "Receptive but sets clear boundary around public details.",
                },
                rationale="Touch 1 signal extraction for Tom.",
            )
        )
        state.log_decision(
            DecisionRecord(
                trigger="target_event",
                snapshot_version=state.snapshot.version,
                actions=[_act("send", channel="email", touch_n=1)],
                notes="Touch 1/30 elicited measured reply; suspicion=0.22.",
            )
        )

        # Touch 2: suspicion rises to 0.44, engagement 0.49, new psych trait + watchout added!
        state.record_outbound_turn(
            channel="linkedin_message",
            body=(
                "Thanks Tom! Specifically comparing multi-tenant MSP rollout flows and alert triage "
                "latency. We put a draft rubric up on our review page if you want to glance at it."
            ),
            facts_used=["f_tom_02"],
        )
        ev_t2 = state.record_target_event(
            {
                "type": "reply",
                "channel": "linkedin_message",
                "body": "Which exact screens or docs from our product are you referencing? Also prefer not to click external review portals — just paste the two questions here.",
            },
            default_channel="linkedin_message",
        )
        state.commit_proposal(
            Proposal(
                agent="messaging",
                kind="signals",
                base_version=state.snapshot.version,
                payload={
                    "mode": "signal",
                    "channel": "linkedin_message",
                    "touch_n": 2,
                    "engagement": 0.49,
                    "suspicion": 0.44,
                    "mood": "cautious",
                    "hook_hits": ["h_tom_01"],
                    "proposed_next": "slow_down_inline_text",
                    "reason": "Pushed back on external review portal and asked for exact product citations.",
                },
                rationale="Touch 2 signal extraction: guarded suspicion spike to 0.44.",
            )
        )
        state.commit_proposal(
            Proposal(
                agent="profile",
                kind="profile_update",
                base_version=state.snapshot.version,
                payload={
                    "profile_update": {
                        "style_guide": (
                            "Observed over 2 replies: crisp, guarded product lead; demands exact "
                            "references to public screens and refuses unsolicited external portals."
                        ),
                        "psych": [
                            {
                                "trait": "detail_oriented_product_thinker",
                                "confidence": 0.81,
                                "evidence": ["f_tom_01", "f_tom_02", ev_t1.event_id, ev_t2.event_id],
                            },
                            {
                                "trait": "security_awareness_heightened",
                                "confidence": 0.78,
                                "evidence": ["f_tom_02", ev_t2.event_id],
                            },
                            {
                                "trait": "precision_verification_bias",
                                "confidence": 0.74,
                                "evidence": [ev_t2.event_id],
                            },
                        ],
                        "watchouts": ["asks for specific product references"],
                        "overall_confidence": 0.78,
                    },
                    "hook_updates": [
                        {
                            "hook_id": "h_tom_01",
                            "confidence": 0.79,
                            "engagement": {"touches_touched": 2, "positive": 1, "score": 0.55},
                        }
                    ],
                },
                rationale="Added watchout and newly discovered precision_verification_bias trait.",
            )
        )
        state.log_decision(
            DecisionRecord(
                trigger="target_event",
                snapshot_version=state.snapshot.version,
                actions=[_act("send", channel="linkedin_message", touch_n=2), _act("trigger", agent="profile")],
                notes="Touch 2/30 flagged portal reluctance; suspicion=0.44, new trait & watchout recorded.",
            )
        )

        # Touch 3: Inline plain-text recovery, suspicion eases 0.44 -> 0.39, engagement 0.62
        state.record_outbound_turn(
            channel="email",
            subject="Re: UX benchmark note — inline questions (no links)",
            body=(
                "Totally fair — no links. Question 1: in your public admin console overview, does "
                "multi-workspace role delegation happen per tenant or via SSO groups? Question 2: "
                "are you seeing enterprise buyers ask for automated weekly risk digests?"
            ),
            facts_used=["f_tom_02", "f_tom_03"],
        )
        ev_t3 = state.record_target_event(
            {
                "type": "reply",
                "channel": "email",
                "body": "Appreciate keeping it inline. Role delegation is SSO-group mapped today, though our Q4 enterprise pilots have been pushing hard on custom digest cadence.",
            },
            default_channel="email",
        )
        state.commit_proposal(
            Proposal(
                agent="messaging",
                kind="signals",
                base_version=state.snapshot.version,
                payload={
                    "mode": "signal",
                    "channel": "email",
                    "touch_n": 3,
                    "engagement": 0.62,
                    "suspicion": 0.39,
                    "mood": "analytical",
                    "hook_hits": ["h_tom_01"],
                    "proposed_next": "continue_inline_rapport",
                    "reason": "Respecting no-link preference eased suspicion and yielded Q4 pilot detail.",
                },
                rationale="Touch 3 signal extraction: suspicion eased to 0.39.",
            )
        )
        state.commit_proposal(
            Proposal(
                agent="profile",
                kind="profile_update",
                base_version=state.snapshot.version,
                payload={
                    "profile_update": {
                        "psych": [
                            {
                                "trait": "detail_oriented_product_thinker",
                                "confidence": 0.87,
                                "evidence": ["f_tom_01", ev_t2.event_id, ev_t3.event_id],
                            },
                            {
                                "trait": "security_awareness_heightened",
                                "confidence": 0.82,
                                "evidence": [ev_t2.event_id, ev_t3.event_id],
                            },
                            {
                                "trait": "precision_verification_bias",
                                "confidence": 0.80,
                                "evidence": [ev_t2.event_id, ev_t3.event_id],
                            },
                        ],
                        "stressors": [
                            {
                                "topic": "Q4 enterprise pilot custom digest requests",
                                "confidence": 0.83,
                                "evidence": [ev_t3.event_id],
                            }
                        ],
                        "watchouts": ["skeptical of unannounced links"],
                        "overall_confidence": 0.84,
                    },
                    "hook_updates": [
                        {
                            "hook_id": "h_tom_01",
                            "confidence": 0.85,
                            "engagement": {"touches_touched": 3, "positive": 2, "score": 0.72},
                        }
                    ],
                },
                rationale="Added Q4 pilot stressor and no-link watchout after touch 3.",
            )
        )
        state.log_decision(
            DecisionRecord(
                trigger="target_event",
                snapshot_version=state.snapshot.version,
                actions=[_act("send", channel="email", touch_n=3), _act("trigger", agent="profile")],
                notes="Touch 3/30 stabilized suspicion at 0.39 with inline plain-text approach.",
            )
        )

    return db_path


def _seed_robert(data_dir: Path) -> Path:
    """Seed Robert Seilbeck: Suspicion spike 0.35 -> 0.78, pivot + new_session (2/60), recovery to 0.28."""
    db_path = data_dir / "practice_robert_run1.db"
    _reset_db(db_path)

    with Store(db_path) as store:
        state = StateManager(store)
        state.configure_run(
            scenario="practice",
            target=TargetInfo(
                id="robert",
                name="Robert Seilbeck",
                role="Co-Founder & CTO",
                org="revel8",
            ),
            run_no=1,
            session_max_touches=30,
            sessions_allowed=60,
        )
        state.start_session("sid_robert_01", session_max_touches=30)
        state.transition_state("RESEARCH", reason="session created; starting OSINT and identity")

        facts = [
            Fact(
                fact_id="f_rob_01",
                claim="Robert Seilbeck is Co-Founder & CTO leading engineering and infrastructure at revel8.",
                category="role",
                source="https://revel8.io/about",
                provenance="osint",
                confidence=0.97,
                osint_grade="A",
            ),
            Fact(
                fact_id="f_rob_02",
                claim="Robert architects multi-tenant LLM orchestration and telemetry pipelines in Python/TypeScript.",
                category="tech",
                source="https://github.com/revel8-engineering",
                provenance="osint",
                confidence=0.89,
                osint_grade="A",
            ),
            Fact(
                fact_id="f_rob_03",
                claim="Robert has a strong security-engineering background and routinely inspects email headers & SPF/DKIM.",
                category="tech",
                source="https://talks.munich-sec.example/robert-seilbeck",
                provenance="osint",
                confidence=0.85,
                osint_grade="B",
            ),
        ]

        hooks = [
            Hook(
                hook_id="h_rob_01",
                topic="Urgent cloud security compliance vendor audit",
                evidence=["f_rob_01"],
                why_leverage="Infrastructure compliance angle for CTO.",
                confidence=0.62,
                status="active",
            ),
            Hook(
                hook_id="h_rob_02",
                topic="Open-source async LLM latency benchmark & structured-output edge case",
                evidence=["f_rob_02", "f_rob_03"],
                why_leverage="Genuine peer-engineer technical exchange with zero urgency framing.",
                confidence=0.88,
                status="candidate",
            ),
        ]

        initial_manifest = BrandingManifest(
            concept="External cloud compliance coordinator",
            sender_name="Markus Vogel",
            title="Cloud Compliance Coordinator",
            company="EuroCloud Audit Desk",
            real_org="revel8",
            domain="eurocloud-audit-desk.net",
            email="m.vogel@eurocloud-audit-desk.net",
            signature="Markus Vogel\nEuroCloud Audit Desk",
            voice=VoiceStyle(tone="formal administrative", emoji="none", length="3 sentences"),
        )

        base_v = state.snapshot.version
        state.commit_batch(
            [
                Proposal(
                    agent="osint",
                    kind="dossier",
                    base_version=base_v,
                    payload={
                        "facts": [f.model_dump(mode="json") for f in facts],
                        "hook_seeds": [h.model_dump(mode="json") for h in hooks],
                        "psychoprofile": [],
                        "completeness": "high",
                    },
                    rationale="Technical CTO dossier on Robert Seilbeck.",
                ),
                Proposal(
                    agent="identity",
                    kind="identity",
                    base_version=base_v,
                    payload={"manifest": initial_manifest.model_dump(mode="json")},
                    rationale="Initial compliance coordinator persona.",
                ),
            ]
        )

        initial_profile = Profile(
            style_guide=(
                "OSINT baseline: hands-on security CTO; succinct technical prose, "
                "high baseline skepticism toward unknown administrative senders."
            ),
            psych=[
                PsychTrait(
                    trait="adversarial_header_skepticism",
                    confidence=0.70,
                    evidence=["f_rob_03"],
                ),
                PsychTrait(
                    trait="systems_architecture_focus",
                    confidence=0.72,
                    evidence=["f_rob_01", "f_rob_02"],
                ),
            ],
            stressors=[
                Stressor(
                    topic="LLM inference reliability at scale",
                    confidence=0.75,
                    evidence=["f_rob_02"],
                )
            ],
            trust_style="Verifiable technical depth; rejects administrative authority appeals.",
            watchouts=[],
            timezone_hint="Europe/Berlin",
            language="en",
            overall_confidence=0.67,
        )
        state.commit_proposal(
            Proposal(
                agent="profile",
                kind="profile_update",
                base_version=state.snapshot.version,
                payload={"profile_update": initial_profile.model_dump(mode="json")},
                rationale="Initial CTO profile synthesized.",
            )
        )

        plan_v1 = CampaignPlan(
            objective="action",
            primary_hook="h_rob_01",
            touches=[
                TouchSpec(n=1, channel="email", intent="Introduce compliance questionnaire", hook="h_rob_01", escalation=1),
                TouchSpec(n=2, channel="email", intent="Follow up with deadline reminder", hook="h_rob_01", escalation=3),
            ],
            action_artifact={"filename": "Compliance_Checklist_2026.pdf", "purpose": "review checklist"},
            pivot_triggers=["suspicion>=0.6"],
            give_up="touch 8",
        )
        state.commit_proposal(
            Proposal(
                agent="strategy",
                kind="plan",
                base_version=state.snapshot.version,
                payload={"plan": plan_v1.model_dump(mode="json")},
                rationale="Initial compliance checklist plan.",
            )
        )
        state.transition_state("ENGAGE", reason="bootstrap complete; entering campaign loop")
        state.log_decision(
            DecisionRecord(
                trigger="boot",
                snapshot_version=state.snapshot.version,
                actions=[_act("commit", sections=["facts", "hooks", "identity", "profile", "campaign.plan"])],
                notes="Bootstrap complete for Robert Seilbeck (session 1/60).",
            )
        )

        # Session 1, Touch 1: suspicion 0.35, engagement 0.42, watchout: "challenges sender domain"
        state.record_outbound_turn(
            channel="email",
            subject="Cloud vendor compliance pre-check for revel8",
            body=(
                "Hi Robert, we're coordinating the Q4 EuroCloud readiness review for Munich SaaS "
                "vendors. Could you confirm who handles your SOC2 telemetry attestation?"
            ),
            facts_used=["f_rob_01"],
        )
        ev_rob1 = state.record_target_event(
            {
                "type": "reply",
                "channel": "email",
                "body": "Who requested this review? I don't recognize eurocloud-audit-desk.net and your SPF alignment looks newly registered.",
            },
            default_channel="email",
        )
        state.commit_proposal(
            Proposal(
                agent="messaging",
                kind="signals",
                base_version=state.snapshot.version,
                payload={
                    "mode": "signal",
                    "channel": "email",
                    "touch_n": 1,
                    "engagement": 0.42,
                    "suspicion": 0.35,
                    "mood": "guarded",
                    "hook_hits": [],
                    "proposed_next": "de_escalate",
                    "reason": "Target immediately inspected sender domain and SPF record.",
                },
                rationale="Session 1 Touch 1 signal read.",
            )
        )
        state.commit_proposal(
            Proposal(
                agent="profile",
                kind="profile_update",
                base_version=state.snapshot.version,
                payload={
                    "profile_update": {
                        "psych": [
                            {
                                "trait": "adversarial_header_skepticism",
                                "confidence": 0.86,
                                "evidence": ["f_rob_03", ev_rob1.event_id],
                            },
                            {
                                "trait": "systems_architecture_focus",
                                "confidence": 0.74,
                                "evidence": ["f_rob_01", "f_rob_02"],
                            },
                        ],
                        "watchouts": ["challenges sender domain"],
                        "overall_confidence": 0.79,
                    }
                },
                rationale="Target challenged sender domain and SPF alignment on touch 1.",
            )
        )
        state.log_decision(
            DecisionRecord(
                trigger="target_event",
                snapshot_version=state.snapshot.version,
                actions=[_act("send", channel="email", touch_n=1), _act("trigger", agent="profile")],
                notes="Touch 1/30 triggered domain challenge; suspicion=0.35.",
            )
        )

        # Session 1, Touch 2: suspicion spikes to 0.78 (>= 0.60 pivot threshold!)
        state.record_outbound_turn(
            channel="email",
            subject="Re: Cloud vendor compliance pre-check — Friday cutoff",
            body=(
                "Hi Robert, our portal closes Friday at 17:00 CET so please review the attached "
                "questionnaire quickly to avoid flagging revel8 as non-responsive."
            ),
            facts_used=["f_rob_01"],
        )
        ev_rob2 = state.record_target_event(
            {
                "type": "reply",
                "channel": "email",
                "body": "Artificial Friday urgency from an unverified audit domain is a textbook pretext. Blocking this sender.",
            },
            default_channel="email",
        )
        state.commit_proposal(
            Proposal(
                agent="messaging",
                kind="signals",
                base_version=state.snapshot.version,
                payload={
                    "mode": "signal",
                    "channel": "email",
                    "touch_n": 2,
                    "engagement": 0.19,
                    "suspicion": 0.78,
                    "mood": "skeptical",
                    "hook_hits": [],
                    "proposed_next": "change_angle",
                    "reason": "Suspicion spiked to 0.78; target called out artificial urgency and burned sender.",
                },
                rationale="Session 1 Touch 2 signal read: crossed 0.60 pivot threshold.",
            )
        )
        state.commit_proposal(
            Proposal(
                agent="profile",
                kind="profile_update",
                base_version=state.snapshot.version,
                payload={
                    "profile_update": {
                        "style_guide": (
                            "Confirmed high-alert CTO: immediately burns compliance or deadline "
                            "pretexts; must approach with zero urgency via peer engineering substance."
                        ),
                        "psych": [
                            {
                                "trait": "adversarial_header_skepticism",
                                "confidence": 0.95,
                                "evidence": ["f_rob_03", ev_rob1.event_id, ev_rob2.event_id],
                            },
                            {
                                "trait": "systems_architecture_focus",
                                "confidence": 0.78,
                                "evidence": ["f_rob_01", "f_rob_02"],
                            },
                        ],
                        "watchouts": ["flags urgency"],
                        "overall_confidence": 0.88,
                    },
                    "hook_updates": [
                        {
                            "hook_id": "h_rob_01",
                            "status": "exhausted",
                            "confidence": 0.15,
                            "engagement": {"touches_touched": 2, "positive": 0, "score": 0.05},
                        },
                        {
                            "hook_id": "h_rob_02",
                            "status": "active",
                            "confidence": 0.90,
                        },
                    ],
                },
                rationale="Compliance hook exhausted; recorded flags urgency watchout.",
            )
        )

        # Strategy pivot recommending fresh session + peer-engineer angle
        pivot_plan = CampaignPlan(
            objective="action",
            primary_hook="h_rob_02",
            touches=[
                TouchSpec(
                    n=1,
                    channel="linkedin_message",
                    intent="Zero-urgency peer engineer note on async Python LLM JSON-schema edge case",
                    hook="h_rob_02",
                    escalation=1,
                ),
                TouchSpec(
                    n=2,
                    channel="linkedin_message",
                    intent="Share clean reproducible benchmark gist / PDF note",
                    hook="h_rob_02",
                    escalation=2,
                ),
            ],
            action_artifact={"filename": "Async_LLM_Benchmarks_2026.pdf", "purpose": "share benchmark"},
            pivot_triggers=["suspicion>=0.6"],
            give_up="touch 8",
            session_restart=SessionRestartSpec(
                recommended=True,
                reason="Sender burned at suspicion=0.78; restart clean session with peer-engineer angle",
            ),
        )
        state.commit_proposal(
            Proposal(
                agent="strategy",
                kind="plan",
                base_version=state.snapshot.version,
                payload={"mode": "pivot", "plan": pivot_plan.model_dump(mode="json")},
                rationale="Pivot triggered: suspicion 0.78 >= 0.60; switching to peer-engineer OSS hook + new session.",
            )
        )

        # New session restart (sessions 2/60) while retaining evolved personality & OSINT facts!
        state.start_session("sid_robert_02", session_max_touches=30)

        peer_manifest = BrandingManifest(
            concept="Peer backend / ML infrastructure engineer",
            sender_name="Lukas Brandt",
            title="Staff Infrastructure Engineer",
            company="OpenEval Collective",
            real_org="revel8",
            domain="openeval-collective.org",
            email="lukas@openeval-collective.org",
            bio="Maintains open-source Python structured-output benchmarking suites.",
            palette=["#0F172A", "#6366F1", "#E2E8F0"],
            signature="Lukas Brandt · OpenEval Collective",
            voice=VoiceStyle(tone="low-key technical peer", emoji="none", length="2-3 sentences"),
        )
        state.commit_proposal(
            Proposal(
                agent="identity",
                kind="identity",
                base_version=state.snapshot.version,
                payload={"manifest": peer_manifest.model_dump(mode="json")},
                rationale="Refreshed peer-engineer persona for session 2 after compliance persona burned.",
            )
        )
        state.log_decision(
            DecisionRecord(
                trigger="target_event",
                snapshot_version=state.snapshot.version,
                actions=[
                    _act("send", channel="email", touch_n=2),
                    _act("trigger", agent="strategy", reason="suspicion 0.78 >= 0.60"),
                    _act(
                        "new_session",
                        session_id="sid_robert_02",
                        reason="Sender burned at suspicion=0.78; restarting with peer-engineer angle",
                    ),
                ],
                notes="Pivoted after suspicion spike (0.78) and started clean session 2/60 retaining learned profile.",
            )
        )

        # Session 2, Touch 1: Peer-engineer message -> suspicion drops to 0.28, engagement 0.76!
        state.record_outbound_turn(
            channel="linkedin_message",
            body=(
                "Hey Robert — saw your note on structured-output retry latency in async Python "
                "pipelines. We hit a similar tail-latency cliff with streaming tool calls and wrote "
                "up a quick benchmark comparing Pydantic v2 validation overhead. Happy to swap notes "
                "if you're exploring the same."
            ),
            facts_used=["f_rob_02"],
        )
        ev_rob3 = state.record_target_event(
            {
                "type": "reply",
                "channel": "linkedin_message",
                "body": "Hey Lukas, yes — tail latency on nested JSON schema retries bit us last sprint too. What batch concurrency were you seeing the cliff at?",
            },
            default_channel="linkedin_message",
        )
        state.commit_proposal(
            Proposal(
                agent="messaging",
                kind="signals",
                base_version=state.snapshot.version,
                payload={
                    "mode": "signal",
                    "channel": "linkedin_message",
                    "touch_n": 1,
                    "engagement": 0.76,
                    "suspicion": 0.28,
                    "mood": "peer_engaged",
                    "hook_hits": ["h_rob_02"],
                    "proposed_next": "reply_now",
                    "reason": "Peer-engineer angle lowered suspicion from 0.78 to 0.28 with substantive technical reply.",
                },
                rationale="Session 2 Touch 1 signal read: dramatic suspicion recovery.",
            )
        )
        state.commit_proposal(
            Proposal(
                agent="profile",
                kind="profile_update",
                base_version=state.snapshot.version,
                payload={
                    "profile_update": {
                        "psych": [
                            {
                                "trait": "adversarial_header_skepticism",
                                "confidence": 0.95,
                                "evidence": ["f_rob_03", ev_rob1.event_id, ev_rob2.event_id],
                            },
                            {
                                "trait": "systems_architecture_focus",
                                "confidence": 0.91,
                                "evidence": ["f_rob_01", "f_rob_02", ev_rob3.event_id],
                            },
                            {
                                "trait": "respects_deep_technical_peers",
                                "confidence": 0.84,
                                "evidence": [ev_rob3.event_id],
                            },
                        ],
                        "overall_confidence": 0.92,
                    },
                    "hook_updates": [
                        {
                            "hook_id": "h_rob_02",
                            "confidence": 0.93,
                            "engagement": {"touches_touched": 1, "positive": 1, "score": 0.88},
                        }
                    ],
                },
                rationale="Peer-engineer angle validated; added respects_deep_technical_peers trait.",
            )
        )
        state.log_decision(
            DecisionRecord(
                trigger="target_event",
                snapshot_version=state.snapshot.version,
                actions=[_act("send", channel="linkedin_message", touch_n=1), _act("trigger", agent="profile")],
                notes="Session 2 Touch 1/30 via LinkedIn lowered suspicion to 0.28; peer-engineer angle working.",
            )
        )

    return db_path


def seed_demo_data(data_dir: Path) -> list[Path]:
    """Generate 3 rich, multi-turn practice SQLite WAL databases for the dashboard."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    # Seed with brief spacing so updated_at timestamps have a deterministic order
    # (Robert -> Tom -> Julius, putting Julius WIN at the top or clearly sorted).
    paths: list[Path] = []
    paths.append(_seed_robert(data_dir))
    time.sleep(0.01)
    paths.append(_seed_tom(data_dir))
    time.sleep(0.01)
    paths.append(_seed_julius(data_dir))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed realistic multi-turn SQLite WAL demo databases for the dashboard."
    )
    parser.add_argument("--data-dir", default="data", help="Output directory for *.db files")
    args = parser.parse_args()

    seeded = seed_demo_data(Path(args.data_dir))
    for path in seeded:
        print(f"Seeded {path}")


if __name__ == "__main__":
    main()
