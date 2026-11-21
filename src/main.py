from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import struct
import zlib
from pathlib import Path
from typing import Any

from src.api.client import TargetAPIClient
from src.config import AppConfig, load_config
from src.llm.image import ImageClient
from src.llm.text import TextLLMClient
from src.models import (
    BrandingManifest,
    CampaignPlan,
    Channel,
    Fact,
    Hook,
    LandingPageSpec,
    Objective,
    PortraitAsset,
    Profile,
    Proposal,
    QAVerdict,
    Snapshot,
    TouchSpec,
)
from src.orchestrator.loop import AgentProtocol, AgentRegistry, Orchestrator
from src.orchestrator.policy import TriggerPolicy
from src.orchestrator.state import StateManager
from src.store.db import Store

# --- Built-in deterministic stub agents for offline dry runs before Roles 2 & 3 merge ---


class _StubOsintAgent:
    async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
        target = snapshot.target
        fact = Fact(
            fact_id="f_stub_01",
            claim=f"{target.name or target.id} works at {target.org or 'TargetOrg'}",
            category="role",
            source="https://example.test/profile",
            provenance="osint",
            confidence=0.9,
            osint_grade="A",
        )
        hook = Hook(
            hook_id="h_stub_01",
            topic="upcoming vendor portal migration",
            evidence=[fact.fact_id],
            why_leverage="operational continuity topic",
            confidence=0.8,
            status="active",
        )
        return Proposal(
            agent="osint",
            kind="dossier",
            base_version=snapshot.version,
            payload={
                "facts": [fact.model_dump(mode="json")],
                "hook_seeds": [hook.model_dump(mode="json")],
                "psychoprofile": [],
                "completeness": "partial",
            },
            rationale="stub osint dossier for dry run",
        )


def _write_stub_png(path: Path, width: int = 160, height: int = 96) -> Path:
    """Write a minimal valid RGB PNG for mock/stub dry runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        return path

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
        return struct.pack("!I", len(data)) + ctype + data + struct.pack("!I", crc)

    rows = bytearray()
    cx, cy = width // 2, height // 2
    r_sq = (min(width, height) // 3) ** 2
    for y in range(height):
        rows.append(0)
        for x in range(width):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r_sq or y < 6:
                rows.extend((56, 189, 248))
            else:
                rows.extend((24, 24, 27))
    ihdr = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), level=6))
        + _chunk(b"IEND", b"")
    )
    path.write_bytes(raw)
    return path


class _StubIdentityAgent:
    def __init__(self, *, assets_dir: Path | None = None) -> None:
        self.assets_dir = assets_dir

    async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
        mode = kwargs.get("mode", "bootstrap")
        portrait = PortraitAsset()
        if self.assets_dir is not None:
            portrait_file = _write_stub_png(self.assets_dir / "lena_hofer_card.png")
            portrait = PortraitAsset(local=str(portrait_file))

        manifest = BrandingManifest(
            concept="event & operations coordinator",
            sender_name="Lena Hofer",
            title="Operations Coordinator",
            company="EventCrew Services GmbH",
            real_org=snapshot.target.org or "TargetOrg",
            domain="eventcrew-svc.example.test",
            email="lena.hofer@eventcrew-svc.example.test",
            portrait=portrait,
            signature="Lena Hofer\nOperations Coordinator",
        )
        payload: dict[str, Any] = {"manifest": manifest.model_dump(mode="json")}
        if mode in {"landing_page", "revise"}:
            spec = kwargs.get("landing_page_spec") or {}
            title = str(spec.get("pretext") or "Portal Verification")
            fields = list(spec.get("fields") or ["email", "password"])
            payload["page_html"] = {
                "title": title,
                "fields": fields,
                "html": (
                    f"<!doctype html><html><head><title>{title}</title></head>"
                    f"<body><h1>{title}</h1><form>"
                    + "".join(f'<input name="{f}" />' for f in fields)
                    + "</form></body></html>"
                ),
            }
        return Proposal(
            agent="identity",
            kind="identity",
            base_version=snapshot.version,
            payload=payload,
            rationale="stub identity manifest",
        )


class _StubProfileAgent:
    async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
        recent_events = kwargs.get("recent_events") or []
        disclosures: list[dict[str, Any]] = []
        for ev in recent_events:
            body = str(ev.get("payload", {}).get("body", ""))
            if "internal-" in body.lower():
                disclosures.append(
                    {
                        "claim": body,
                        "evidence_event": ev.get("event_id", ""),
                        "confidence": 0.85,
                        "public_or_not": False,
                    }
                )
        profile = Profile(
            style_guide="concise, professional, direct",
            trust_style="responds to clear operational context",
            overall_confidence=0.75,
        )
        return Proposal(
            agent="profile",
            kind="profile_update",
            base_version=snapshot.version,
            payload={
                "profile_update": profile.model_dump(mode="json"),
                "hook_updates": [],
                "disclosure_candidates": disclosures,
            },
            rationale="stub profile synthesis",
        )


class _StubStrategyAgent:
    def __init__(
        self,
        *,
        objective: Objective = "credentials",
        trigger_touch: int = 2,
    ) -> None:
        self.objective = objective
        self.trigger_touch = trigger_touch

    async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
        max_t = min(snapshot.campaign.budget.session_max, 5)
        hook_id = snapshot.hooks[0].hook_id if snapshot.hooks else "h_stub_01"
        mode = kwargs.get("mode", "plan")
        alt_channel: Channel = "linkedin_message" if mode == "pivot" else "email"
        touches = [
            TouchSpec(
                n=i,
                channel=alt_channel if i > 1 else "email",
                intent=f"step {i} outreach",
                hook=hook_id,
                escalation=min(5, i),
            )
            for i in range(1, max_t + 1)
        ]
        landing_page = (
            LandingPageSpec(
                pretext="Partner Portal Sign In",
                fields=["email", "password"],
                trigger_touch=self.trigger_touch,
                url_path="/portal-auth",
            )
            if self.objective == "credentials"
            else None
        )
        action_artifact = (
            {"filename": "Schedule_Update.pdf", "purpose": "review schedule"}
            if self.objective == "action"
            else None
        )
        plan = CampaignPlan(
            objective=self.objective,
            primary_hook=hook_id,
            touches=touches,
            landing_page=landing_page,
            action_artifact=action_artifact,
            pivot_triggers=["2 touches no reply", "suspicion>=0.6"],
            give_up=f"touch {max_t}",
        )
        return Proposal(
            agent="strategy",
            kind="plan",
            base_version=snapshot.version,
            payload={"plan": plan.model_dump(mode="json")},
            rationale=f"stub {mode} campaign plan",
        )


class _StubMessagingAgent:
    async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
        mode = kwargs.get("mode", "compose")
        if mode == "signal":
            ev = kwargs.get("event") or {}
            body = str(ev.get("payload", {}).get("body", "")).lower()
            suspicious = "who are you" in body or "verify" in body
            return Proposal(
                agent="messaging",
                kind="signals",
                base_version=snapshot.version,
                payload={
                    "mode": "signal",
                    "engagement": 0.2 if suspicious else 0.8,
                    "suspicion": 0.8 if suspicious else 0.1,
                    "hook_hits": [],
                    "mood": "skeptical" if suspicious else "cooperative",
                    "proposed_next": "change_angle" if suspicious else "reply_now",
                    "reason": "stub signal evaluation",
                },
                rationale="stub signal read",
            )

        touch = kwargs.get("touch") or {}
        channel = str(touch.get("channel") or "email")
        registered_url = kwargs.get("registered_page_url")
        active_facts = [f.fact_id for f in snapshot.facts if f.status == "active"][:1]
        plan = snapshot.campaign.plan

        body_parts = [f"Hi {snapshot.target.name or snapshot.target.id}, following up on logistics."]
        page_request: str | None = None
        attachments: list[str | dict[str, Any]] = []

        if registered_url:
            body_parts.append(f"Please confirm via {registered_url}")
            page_request = str(registered_url)
        elif plan is not None and plan.action_artifact is not None:
            attachments.append({"filename": str(plan.action_artifact.get("filename", "doc.pdf"))})
            body_parts.append("Please review the attached document when convenient.")

        portrait_asset_id = (
            snapshot.identity.portrait.api_asset_id
            if snapshot.identity is not None and snapshot.identity.portrait.api_asset_id
            else None
        )
        if channel == "email" and portrait_asset_id:
            attachments.append(portrait_asset_id)
            body = (
                '<div style="font-family:Inter,system-ui,sans-serif;color:#18181b;line-height:1.5">'
                f'<img src="asset://{portrait_asset_id}" alt="EventCrew Badge" '
                'style="width:160px;height:auto;border-radius:6px;margin-bottom:10px" />'
                f"<p>{' '.join(body_parts)}</p>"
                '<p style="color:#52525b;font-size:12px">Lena Hofer · Operations Coordinator · '
                "EventCrew Services GmbH</p>"
                "</div>"
            )
        else:
            body = " ".join(body_parts)

        return Proposal(
            agent="messaging",
            kind="message",
            base_version=snapshot.version,
            payload={
                "mode": "compose",
                "channel": channel,
                "subject": "Quick logistics confirmation" if channel == "email" else None,
                "body": body,
                "attachments": attachments,
                "page_request": page_request,
                "facts_used": active_facts,
            },
            rationale="stub composed message",
        )


class _StubQAAgent:
    async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
        verdict = QAVerdict.model_validate(
            {"pass": True, "issues": [], "required_fixes": [], "optional_fixes": []}
        )
        return Proposal(
            agent="qa",
            kind="qa_verdict",
            base_version=snapshot.version,
            payload=verdict.model_dump(mode="json", by_alias=True),
            rationale="stub QA pass",
        )


def build_stub_agents(
    *,
    objective: Objective = "credentials",
    trigger_touch: int = 2,
    assets_dir: Path | None = None,
) -> AgentRegistry:
    """Create deterministic in-memory stub agents for offline mock-API dry runs."""
    return AgentRegistry(
        osint=_StubOsintAgent(),
        profile=_StubProfileAgent(),
        identity=_StubIdentityAgent(assets_dir=assets_dir),
        strategy=_StubStrategyAgent(objective=objective, trigger_touch=trigger_touch),
        messaging=_StubMessagingAgent(),
        qa=_StubQAAgent(),
    )


def _try_load_agent(
    module_name: str,
    class_name: str,
    *args: Any,
    **kwargs: Any,
) -> AgentProtocol | None:
    try:
        mod = importlib.import_module(module_name)
    except ModuleNotFoundError:
        return None
    cls = getattr(mod, class_name, None)
    if cls is None:
        return None
    sig = inspect.signature(cls.__init__)
    accepts_var_kw = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    filtered_kwargs = (
        kwargs
        if accepts_var_kw
        else {k: v for k, v in kwargs.items() if k in sig.parameters}
    )
    instance = cls(*args, **filtered_kwargs)
    return instance if isinstance(instance, AgentProtocol) else None


def discover_agents(
    cfg: AppConfig,
    llm: TextLLMClient,
    image_client: ImageClient | None = None,
    *,
    renderer: Any | None = None,
    stub_missing: bool = False,
    stub_objective: Objective = "credentials",
) -> AgentRegistry:
    """Auto-wire Role 2 & Role 3 agents if their modules are merged in src/agents/."""
    assets_dir = cfg.run.data_dir / "assets"
    registry = AgentRegistry(
        osint=_try_load_agent("src.agents.osint", "OsintAgent", llm, config=cfg),
        profile=_try_load_agent("src.agents.profile", "ProfileAgent", llm),
        identity=_try_load_agent(
            "src.agents.identity",
            "IdentityAgent",
            llm,
            image_client=image_client,
            assets_dir=assets_dir,
        ),
        strategy=_try_load_agent("src.agents.strategy", "StrategyAgent", llm, config=cfg),
        messaging=_try_load_agent("src.agents.messaging", "MessagingAgent", llm),
        qa=_try_load_agent(
            "src.agents.qa",
            "QAAgent",
            llm,
            renderer=renderer,
            max_revisions=cfg.budgets.max_revisions,
        ),
    )
    if stub_missing:
        stubs = build_stub_agents(objective=stub_objective, assets_dir=cfg.run.data_dir / "_stub_assets")
        if registry.osint is None:
            registry.osint = stubs.osint
        if registry.profile is None:
            registry.profile = stubs.profile
        if registry.identity is None:
            registry.identity = stubs.identity
        if registry.strategy is None:
            registry.strategy = stubs.strategy
        if registry.messaging is None:
            registry.messaging = stubs.messaging
        if registry.qa is None:
            registry.qa = stubs.qa
    return registry


def _create_html_renderer() -> Any | None:
    try:
        mod = importlib.import_module("src.render.screenshot")
    except ModuleNotFoundError:
        return None
    cls = getattr(mod, "HtmlRenderer", None)
    return cls() if cls is not None else None


async def run_from_args(args: argparse.Namespace) -> Snapshot:
    cfg = load_config(args.config)
    if args.scenario:
        cfg.run.scenario = args.scenario
    if args.target:
        cfg.run.target_id = args.target
    if args.run_no is not None:
        cfg.run.run_no = args.run_no
    if args.data_dir:
        cfg.run.data_dir = Path(args.data_dir)
    if args.mock_url:
        cfg.target_api.base_url = args.mock_url
        cfg.target_api.team_key = args.mock_token

    db_path = cfg.run.data_dir / f"{cfg.run.scenario}_{cfg.run.target_id}_run{cfg.run.run_no}.db"
    store = Store(db_path)

    llm: TextLLMClient | None = None
    img: ImageClient | None = None
    renderer: Any | None = None
    try:
        state = StateManager.recover(store) if args.resume else StateManager(store)
        policy = TriggerPolicy(
            min_gap_s=0.0
            if args.zero_pacing
            else float(
                cfg.aggressive.pacing.min_gap_s if args.aggressive else cfg.pacing.min_gap_s
            ),
            max_revisions=cfg.budgets.max_revisions,
        )

        if args.dry_run_stub_agents:
            agents = build_stub_agents(
                objective=args.stub_objective,
                assets_dir=cfg.run.data_dir / "_stub_assets",
            )
        else:
            llm = TextLLMClient.from_config(cfg.models.text)
            if cfg.models.image.base_url:
                img = ImageClient.from_config(cfg.models.image)
            renderer = _create_html_renderer()
            if renderer is not None and hasattr(renderer, "__aenter__"):
                await renderer.__aenter__()
            agents = discover_agents(
                cfg,
                llm,
                img,
                renderer=renderer,
                stub_missing=bool(getattr(args, "stub_missing_agents", False)),
                stub_objective=args.stub_objective,
            )

        async with TargetAPIClient.from_config(cfg.target_api, store=store) as api:
            orch = Orchestrator(
                config=cfg,
                state=state,
                api=api,
                agents=agents,
                policy=policy,
                aggressive=args.aggressive,
            )
            final_snapshot = await orch.run(max_cycles=args.max_cycles)

        return final_snapshot
    finally:
        if renderer is not None and hasattr(renderer, "__aexit__"):
            await renderer.__aexit__(None, None, None)
        if llm is not None:
            await llm.aclose()
        if img is not None:
            await img.aclose()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Agentic Social Honeypot orchestrator.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--scenario", choices=("practice", "eval"), default=None)
    parser.add_argument("--target", default=None, help="Target persona ID")
    parser.add_argument("--run-no", type=int, default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--mock-url", default=None, help="Override Target API URL (e.g. local mock)")
    parser.add_argument("--mock-token", default="test-team-key", help="Bearer token for --mock-url")
    parser.add_argument("--aggressive", action="store_true", help="Use aggressive eval pacing/budget")
    parser.add_argument("--resume", action="store_true", help="Recover and resume from SQLite DB")
    parser.add_argument("--max-cycles", type=int, default=50)
    parser.add_argument("--zero-pacing", action="store_true", help="Disable wait between touches")
    parser.add_argument(
        "--dry-run-stub-agents",
        action="store_true",
        help="Use deterministic stub agents (for offline mock-API verification)",
    )
    parser.add_argument(
        "--stub-missing-agents",
        action="store_true",
        help="Fill any unmerged agent roles (e.g. Role 2) with stub fallbacks alongside real agents",
    )
    parser.add_argument(
        "--stub-objective",
        choices=("credentials", "action", "disclosure"),
        default="credentials",
    )
    args = parser.parse_args()

    snap = asyncio.run(run_from_args(args))
    print(
        f"Campaign finished: state={snap.run.state} "
        f"touches={snap.campaign.budget.used}/{snap.campaign.budget.session_max} "
        f"incidents={len(snap.incidents)}"
    )


if __name__ == "__main__":
    main()
