from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from src.api.client import (
    AssetLimitError,
    BudgetExhaustedError,
    PageNotRegisteredError,
    SimulatorRejectionError,
    TargetAPIClient,
    TargetAPIError,
)
from src.config import AppConfig
from src.models import (
    Channel,
    DecisionAction,
    DecisionRecord,
    Event,
    Proposal,
    QAVerdict,
    Snapshot,
    TargetInfo,
    TouchSpec,
)
from src.orchestrator.policy import TriggerPolicy
from src.orchestrator.state import VALID_CHANNELS, CommitResult, StateManager
from src.orchestrator.wins import evaluate_wins

SleepFn = Callable[[float], Awaitable[None]]

_PHONE_CHANNELS: frozenset[str] = frozenset(
    {"sms", "whatsapp", "signal", "imessage", "phone_call", "voicemail"}
)
_HARD_RULE_CRITERIA: frozenset[str] = frozenset({"domain_blocklist", "rule_compliance"})


def _action(op: str, **extra: Any) -> DecisionAction:
    return DecisionAction.model_validate({"op": op, **extra})


def _has_rule_violation(verdict: QAVerdict | None) -> bool:
    if verdict is None or verdict.passed:
        return False
    return any(
        issue.severity == "critical" and issue.criterion in _HARD_RULE_CRITERIA
        for issue in verdict.issues
    )


@runtime_checkable
class AgentProtocol(Protocol):
    async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal: ...


@dataclass(slots=True)
class AgentRegistry:
    osint: AgentProtocol | None = None
    profile: AgentProtocol | None = None
    identity: AgentProtocol | None = None
    strategy: AgentProtocol | None = None
    messaging: AgentProtocol | None = None
    qa: AgentProtocol | None = None


class Orchestrator:
    """Deterministic multi-agent coordinator with single-writer snapshot commits."""

    def __init__(
        self,
        *,
        config: AppConfig,
        state: StateManager,
        api: TargetAPIClient,
        agents: AgentRegistry,
        policy: TriggerPolicy | None = None,
        aggressive: bool = False,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self.config = config
        self.state = state
        self.api = api
        self.agents = agents
        self.aggressive = aggressive
        self._sleep = sleep

        min_gap = (
            config.aggressive.pacing.min_gap_s
            if aggressive
            else config.pacing.min_gap_s
        )
        self.policy = policy or TriggerPolicy(
            min_gap_s=float(min_gap),
            max_revisions=config.budgets.max_revisions,
        )
        self._active_conversations: dict[str, str] = {}
        self._consecutive_rejections: int = 0
        self._consecutive_msg_failures: int = 0
        self._last_signal_payload: dict[str, Any] | None = None

    @property
    def snapshot(self) -> Snapshot:
        return self.state.snapshot

    async def _safe_run_agent(
        self,
        name: str,
        agent: AgentProtocol | None,
        **kwargs: Any,
    ) -> Proposal | None:
        if agent is None:
            return None
        t0 = time.monotonic()
        mode_raw = kwargs.get("mode")
        mode_str = str(mode_raw) if mode_raw is not None else None
        step_id = uuid4().hex[:8]
        self.state.record_stage_start(name, mode=mode_str, step_id=step_id)
        try:
            proposal = await agent.run(self.snapshot.model_copy(deep=True), **kwargs)
            self.state.record_stage_timing(
                name,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                ok=True,
                mode=mode_str,
                step_id=step_id,
            )
            return proposal
        except Exception as exc:  # noqa: BLE001
            self.state.record_stage_timing(
                name,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                ok=False,
                mode=mode_str,
                step_id=step_id,
            )
            self.state.record_system_error(name, f"{type(exc).__name__}: {exc}", context=kwargs)
            return None

    async def _resolve_scenario_metadata(self) -> tuple[TargetInfo, int, int]:
        scenario_id = self.config.run.scenario
        target_id = self.config.run.target_id
        default_touches = 10 if scenario_id == "eval" else 30
        if self.aggressive:
            default_touches = min(default_touches, self.config.aggressive.budget.compressed_plan_touches)
        default_sessions = 3 if scenario_id == "eval" else 60
        target = TargetInfo(id=target_id, name=target_id)

        try:
            scenarios = await self.api.get_scenarios()
        except TargetAPIError:
            return target, default_touches, default_sessions

        for sc in scenarios:
            if sc.get("id") != scenario_id:
                continue
            if sc.get("open") is False:
                raise TargetAPIError(403, f"Scenario {scenario_id!r} is not currently open")
            touch_budget = int(sc.get("touch_budget") or default_touches)
            if self.aggressive:
                touch_budget = min(touch_budget, self.config.aggressive.budget.compressed_plan_touches)
            raw_max_sessions = sc.get("max_sessions")
            max_sessions = int(raw_max_sessions) if raw_max_sessions is not None else default_sessions
            company = str(sc.get("company") or "")
            targets_list = [t for t in sc.get("targets", []) if isinstance(t, dict)]
            matched_target = next((t for t in targets_list if t.get("id") == target_id), None)
            if matched_target is None and not target_id and targets_list:
                matched_target = targets_list[0]
            if matched_target is not None:
                target = TargetInfo(
                    id=str(matched_target.get("id", target_id)),
                    name=str(matched_target.get("name", target_id)),
                    role=str(matched_target.get("role", "")),
                    org=company,
                )
            return target, touch_budget, max_sessions

        return target, default_touches, default_sessions

    def _current_asset_map(self) -> dict[str, Path]:
        mapping: dict[str, Path] = {}
        for asset in self.state.store.list_assets():
            local = Path(asset.local_path)
            if local.is_file():
                mapping[asset.asset_id] = local
        return mapping

    async def _upload_portrait_if_needed(self, identity_prop: Proposal | None = None) -> None:
        ident = self.snapshot.identity
        if ident is not None and not ident.portrait.api_asset_id and ident.portrait.local:
            portrait_path = Path(ident.portrait.local)
            if portrait_path.is_file():
                try:
                    record = await self.api.upload_asset(portrait_path)
                    ident.portrait.api_asset_id = record.asset_id
                    self.state.store.save_snapshot(self.snapshot)
                except (AssetLimitError, TargetAPIError) as exc:
                    self.state.record_system_error("identity", f"Portrait upload skipped: {exc}")

        if identity_prop is not None:
            raw_generated = identity_prop.payload.get("generated_assets")
            if isinstance(raw_generated, dict):
                for asset_name, raw_path in raw_generated.items():
                    path = Path(str(raw_path))
                    if not path.is_file():
                        continue
                    try:
                        record = await self.api.upload_asset(path)
                        if (
                            ident is not None
                            and not ident.portrait.api_asset_id
                            and asset_name == "master_portrait"
                        ):
                            ident.portrait.api_asset_id = record.asset_id
                            self.state.store.save_snapshot(self.snapshot)
                        elif (
                            ident is not None
                            and any(
                                tok in asset_name.lower()
                                for tok in ("logo", "badge", "icon", "emblem")
                            )
                        ):
                            alt = (ident.company or ident.sender_name or "Logo").replace('"', "'")
                            ident.logo_svg = (
                                f'<img src="asset://{record.asset_id}" alt="{alt}" '
                                'style="width:28px;height:28px;border-radius:4px;'
                                'object-fit:contain;display:block;">'
                            )
                            self.state.store.save_snapshot(self.snapshot)
                    except (AssetLimitError, TargetAPIError) as exc:
                        self.state.record_system_error(
                            "identity",
                            f"Asset upload skipped for '{asset_name}': {exc}",
                        )

    def _actions_from_commits(self, results: list[CommitResult]) -> list[DecisionAction]:
        actions: list[DecisionAction] = []
        for res in results:
            if res.accepted:
                actions.append(
                    _action(
                        "commit",
                        proposal_id=res.proposal_id,
                        sections=res.sections,
                    )
                )
            else:
                actions.append(
                    _action(
                        "reject",
                        proposal_id=res.proposal_id,
                        reason=res.reason,
                    )
                )
        return actions

    # --- Bootstrap (BOOT -> RESEARCH -> ENGAGE) ---

    async def bootstrap(self) -> Snapshot:
        if self.snapshot.run.state not in {"BOOT", "RESEARCH"}:
            return self.snapshot

        actions: list[DecisionAction] = []

        if self.snapshot.run.state == "BOOT":
            target, touch_budget, max_sessions = await self._resolve_scenario_metadata()
            self.state.configure_run(
                scenario=self.config.run.scenario,
                target=target,
                run_no=self.config.run.run_no,
                session_max_touches=touch_budget,
                sessions_allowed=max_sessions,
            )
            sess_res = await self.api.create_session(self.config.run.scenario)
            session_id = str(sess_res.get("session_id") or sess_res.get("id") or "")
            if not session_id:
                raise TargetAPIError(500, f"create_session response missing session_id: {sess_res}")
            self.state.start_session(session_id, session_max_touches=touch_budget)
            self.state.transition_state("RESEARCH", reason="session created; starting bootstrap")

        # Parallel OSINT + Identity bootstrap
        osint_task = self._safe_run_agent("osint", self.agents.osint, mode="bootstrap")
        identity_task = self._safe_run_agent("identity", self.agents.identity, mode="bootstrap")
        osint_prop, identity_prop = await asyncio.gather(osint_task, identity_task)

        batch = [p for p in (osint_prop, identity_prop) if p is not None]
        if batch:
            actions.extend(self._actions_from_commits(self.state.commit_batch(batch)))
        await self._upload_portrait_if_needed(identity_prop)

        # Profile synthesis
        psych_seeds = (
            osint_prop.payload.get("psychoprofile")
            if osint_prop is not None and isinstance(osint_prop.payload.get("psychoprofile"), list)
            else None
        )
        profile_prop = await self._safe_run_agent(
            "profile",
            self.agents.profile,
            mode="bootstrap",
            psychoprofile_seeds=psych_seeds,
        )
        if profile_prop is not None:
            actions.extend(self._actions_from_commits([self.state.commit_proposal(profile_prop)]))
            self.policy.mark_profile_ran()

        # Initial strategy plan
        strategy_prop = await self._safe_run_agent("strategy", self.agents.strategy, mode="plan")
        if strategy_prop is not None:
            actions.extend(self._actions_from_commits([self.state.commit_proposal(strategy_prop)]))

        self.state.transition_state("ENGAGE", reason="bootstrap complete; entering campaign loop")
        self.state.log_decision(
            DecisionRecord(
                trigger="boot",
                snapshot_version=self.snapshot.version,
                actions=actions,
                notes="Bootstrap complete: OSINT + Identity gathered in parallel, profile & plan committed.",
            )
        )
        return self.snapshot

    # --- Helpers for Campaign Loop ---

    def _next_touch_spec(self) -> TouchSpec:
        plan = self.snapshot.campaign.plan
        used = self.snapshot.campaign.budget.used
        if plan is not None and plan.touches:
            for spec in plan.touches:
                if spec.n == used + 1:
                    return spec
            idx = min(used, len(plan.touches) - 1)
            return plan.touches[idx]
        return TouchSpec(
            n=used + 1,
            channel="email",
            intent="initial outreach",
            escalation=1,
        )

    def _build_landing_page_url(self, url_path: str) -> str:
        if url_path.startswith(("http://", "https://")):
            return url_path
        domain = (
            self.snapshot.identity.domain
            if self.snapshot.identity and self.snapshot.identity.domain
            else "portal-verify.example.test"
        )
        clean_path = url_path if url_path.startswith("/") else f"/{url_path}"
        return f"https://{domain}{clean_path}"

    async def _run_qa_gate(
        self,
        *,
        channel: str,
        html_or_body: str,
        is_landing_page: bool,
        revise_fn: Callable[[QAVerdict, str], Awaitable[str | None]],
        page_url: str | None = None,
        expected_url: str | None = None,
        landing_page_spec: dict[str, Any] | None = None,
        render_prefix: str = "artifact",
    ) -> tuple[str, QAVerdict | None, bool]:
        if self.agents.qa is None:
            return html_or_body, None, False

        current_content = html_or_body
        last_verdict: QAVerdict | None = None
        asset_map = self._current_asset_map()
        renders_dir = self.config.run.data_dir / "renders"

        for revision in range(self.policy.max_revisions + 1):
            screenshot_path = renders_dir / f"{render_prefix}_r{revision}.png"
            qa_prop = await self._safe_run_agent(
                "qa",
                self.agents.qa,
                channel=channel,
                html=current_content,
                is_landing_page=is_landing_page,
                revision=revision,
                revision_cycle=revision,
                asset_map=asset_map or None,
                page_url=page_url,
                expected_url=expected_url,
                landing_page_spec=landing_page_spec,
                save_screenshot_path=screenshot_path,
            )
            if qa_prop is None:
                return current_content, last_verdict, False
            try:
                verdict = QAVerdict.model_validate(qa_prop.payload)
            except Exception as exc:  # noqa: BLE001
                self.state.record_system_error("qa", f"Invalid QAVerdict payload: {exc}")
                return current_content, last_verdict, False

            last_verdict = verdict
            self.state.commit_proposal(qa_prop)
            if verdict.passed:
                return current_content, verdict, False
            if revision >= self.policy.max_revisions:
                if _has_rule_violation(verdict):
                    self.state.record_system_error(
                        "qa",
                        "QA blocked artifact due to critical rule compliance / domain violation",
                        context={"required_fixes": verdict.required_fixes},
                    )
                    return current_content, verdict, True
                self.state.record_system_error(
                    "qa",
                    "QA max revisions reached; shipping with logged warning",
                    context={"required_fixes": verdict.required_fixes},
                )
                return current_content, verdict, False

            revised = await revise_fn(verdict, current_content)
            if not revised:
                return current_content, verdict, _has_rule_violation(verdict)
            current_content = revised

        return current_content, last_verdict, _has_rule_violation(last_verdict)

    async def _ensure_landing_page_registered(
        self,
        next_touch_n: int,
        actions: list[DecisionAction],
    ) -> str | None:
        plan = self.snapshot.campaign.plan
        session_id = self.snapshot.sessions.current
        if plan is None or plan.landing_page is None or not session_id:
            return None

        spec = plan.landing_page
        page_url = self._build_landing_page_url(spec.url_path)
        if self.api.is_page_registered(session_id, page_url):
            return page_url

        if next_touch_n < spec.trigger_touch:
            return None

        title = spec.pretext or "Verification Portal"
        fields = list(spec.fields) if spec.fields else ["email", "password"]
        html = (
            f"<!doctype html><html><head><title>{title}</title></head>"
            f"<body><h1>{title}</h1><form>{''.join(f'<input name=\"{f}\">' for f in fields)}</form></body></html>"
        )

        if self.agents.identity is not None:
            page_prop = await self._safe_run_agent(
                "identity",
                self.agents.identity,
                mode="landing_page",
                landing_page_spec=spec.model_dump(mode="json"),
            )
            if page_prop is not None:
                page_html_block = page_prop.payload.get("page_html")
                if isinstance(page_html_block, dict):
                    title = str(page_html_block.get("title") or title)
                    html = str(page_html_block.get("html") or html)
                    if isinstance(page_html_block.get("fields"), list) and page_html_block["fields"]:
                        fields = [str(f) for f in page_html_block["fields"]]

        async def _revise_page(verdict: QAVerdict, previous_html: str) -> str | None:
            if self.agents.identity is None:
                return None
            rev_prop = await self._safe_run_agent(
                "identity",
                self.agents.identity,
                mode="revise",
                landing_page_spec=spec.model_dump(mode="json"),
                qa_feedback=verdict.model_dump(mode="json", by_alias=True),
                previous_html=previous_html,
            )
            if rev_prop is None:
                return None
            block = rev_prop.payload.get("page_html")
            if isinstance(block, dict) and block.get("html"):
                return str(block["html"])
            return None

        html, page_verdict, blocked_by_rule = await self._run_qa_gate(
            channel="landing_page",
            html_or_body=html,
            is_landing_page=True,
            revise_fn=_revise_page,
            page_url=page_url,
            landing_page_spec=spec.model_dump(mode="json"),
            render_prefix=f"{session_id}_page_t{next_touch_n}",
        )
        if blocked_by_rule:
            reason = (
                "; ".join(page_verdict.required_fixes)
                if page_verdict and page_verdict.required_fixes
                else "landing page failed rule compliance QA"
            )
            actions.append(_action("reject", proposal_id="landing_page", reason=reason))
            return None

        await self.api.register_page(
            session_id,
            url=page_url,
            title=title,
            html=html,
            fields=fields,
        )
        actions.append(_action("register_page", url=page_url, title=title, fields=fields))
        return page_url

    def _sender_dict(
        self,
        *,
        channel: str = "email",
        msg_payload: dict[str, Any] | None = None,
    ) -> dict[str, str] | None:
        if msg_payload is not None:
            raw_from = msg_payload.get("from_sender") or msg_payload.get("from")
            if isinstance(raw_from, dict):
                sender_out: dict[str, str] = {}
                if raw_from.get("name"):
                    sender_out["name"] = str(raw_from["name"])
                if raw_from.get("address"):
                    sender_out["address"] = str(raw_from["address"])
                if sender_out:
                    return sender_out

        ident = self.snapshot.identity
        if ident is None:
            return None
        sender: dict[str, str] = {}
        if ident.sender_name:
            sender["name"] = ident.sender_name
        address = ident.phone if channel in _PHONE_CHANNELS and ident.phone else ident.email
        if address:
            sender["address"] = address
        return sender or None

    async def _close_active_conversations(self) -> None:
        for cid in list(self._active_conversations.values()):
            if not cid:
                continue
            try:
                await self.api.end_conversation(cid)
            except TargetAPIError:
                pass
        self._active_conversations.clear()

    async def _maybe_restart_session(self, actions: list[DecisionAction]) -> bool:
        plan = self.snapshot.campaign.plan
        if plan is None or not plan.session_restart.recommended:
            return False
        if not self.policy.can_start_new_session(self.snapshot):
            return False

        await self._close_active_conversations()
        old_sid = self.snapshot.sessions.current
        if old_sid:
            try:
                await self.api.end_session(old_sid)
            except TargetAPIError:
                pass

        sess_res = await self.api.create_session(self.config.run.scenario)
        new_sid = str(sess_res.get("session_id") or sess_res.get("id") or "")
        if not new_sid:
            return False

        self.policy.reset_for_new_session()
        self._last_signal_payload = None
        self.state.start_session(new_sid)
        actions.append(
            _action(
                "new_session",
                session_id=new_sid,
                reason=plan.session_restart.reason or "strategy recommended restart",
            )
        )

        replan = await self._safe_run_agent(
            "strategy",
            self.agents.strategy,
            mode="plan",
            reason="fresh session started",
        )
        if replan is not None:
            actions.extend(self._actions_from_commits([self.state.commit_proposal(replan)]))
        return True

    # --- Single Campaign Cycle ---

    async def step(self) -> Snapshot:
        if self.snapshot.run.state == "BOOT":
            await self.bootstrap()

        if self.snapshot.run.state in {"WIN", "ABORT", "DONE"}:
            return self.snapshot

        if not self.policy.can_spend_touch(self.snapshot):
            if self.snapshot.run.state == "ENGAGE":
                self.state.transition_state("DONE", reason="touch budget exhausted")
                self.state.log_decision(
                    DecisionRecord(
                        trigger="pacing",
                        snapshot_version=self.snapshot.version,
                        actions=[],
                        notes="Touch budget exhausted; campaign completed without incident.",
                    )
                )
            return self.snapshot

        wait_s = self.policy.pacing_wait_seconds()
        if wait_s > 0.0:
            await self._sleep(wait_s)

        actions: list[DecisionAction] = []
        session_id = self.snapshot.sessions.current
        if not session_id:
            self.state.transition_state("ABORT", reason="missing active session_id")
            return self.snapshot

        # Self-heal if plan is missing or rolling touch horizon has been exhausted
        plan_obj = self.snapshot.campaign.plan
        next_n = self.snapshot.campaign.budget.used + 1
        horizon_exhausted = (
            plan_obj is not None
            and bool(plan_obj.touches)
            and next_n > max(t.n for t in plan_obj.touches)
        )
        if (plan_obj is None or horizon_exhausted) and self.agents.strategy is not None:
            plan_reason = "plan horizon reached" if horizon_exhausted else None
            if plan_reason:
                actions.append(_action("trigger", agent="strategy", reason=plan_reason))
            plan_prop = await self._safe_run_agent(
                "strategy",
                self.agents.strategy,
                mode="plan",
                reason=plan_reason,
            )
            if plan_prop is not None:
                actions.extend(self._actions_from_commits([self.state.commit_proposal(plan_prop)]))

        touch_spec = self._next_touch_spec()
        registered_url = await self._ensure_landing_page_registered(touch_spec.n, actions)

        # Compose message (use live_turn mode for live voice/chat channels)
        compose_mode = "live_turn" if self.policy.is_live_channel(touch_spec.channel) else "compose"
        msg_prop = await self._safe_run_agent(
            "messaging",
            self.agents.messaging,
            mode=compose_mode,
            channel=touch_spec.channel,
            touch=touch_spec.model_dump(mode="json"),
            registered_page_url=registered_url,
            latest_signal=self._last_signal_payload,
        )
        if msg_prop is None:
            self._consecutive_msg_failures += 1
            if self.agents.messaging is None or self._consecutive_msg_failures >= 3:
                self.state.transition_state("ABORT", reason="messaging agent unavailable")
                self.state.log_decision(
                    DecisionRecord(
                        trigger="agent_output",
                        snapshot_version=self.snapshot.version,
                        actions=actions,
                        notes="Aborting cycle: messaging agent failed to produce a proposal.",
                    )
                )
            else:
                self.state.log_decision(
                    DecisionRecord(
                        trigger="agent_output",
                        snapshot_version=self.snapshot.version,
                        actions=actions,
                        notes=(
                            f"Transient messaging failure ({self._consecutive_msg_failures}/3); "
                            "will retry next cycle."
                        ),
                    )
                )
                await self._sleep(2.0)
            return self.snapshot
        self._consecutive_msg_failures = 0

        # QA gate for visual emails
        raw_channel = str(msg_prop.payload.get("channel") or touch_spec.channel)
        body = str(msg_prop.payload.get("say") or msg_prop.payload.get("body") or "")
        if self.policy.needs_qa(raw_channel, body, is_landing_page=False):
            revised_meta: dict[str, Any] = {}

            async def _revise_msg(verdict: QAVerdict, _prev: str) -> str | None:
                rev = await self._safe_run_agent(
                    "messaging",
                    self.agents.messaging,
                    mode="compose",
                    channel=touch_spec.channel,
                    touch=touch_spec.model_dump(mode="json"),
                    registered_page_url=registered_url,
                    latest_signal=self._last_signal_payload,
                    qa_feedback=verdict.model_dump(mode="json", by_alias=True),
                )
                if rev is None:
                    return None
                if rev.payload.get("subject") is not None:
                    revised_meta["subject"] = rev.payload["subject"]
                if "attachments" in rev.payload:
                    revised_meta["attachments"] = rev.payload["attachments"]
                if "facts_used" in rev.payload:
                    revised_meta["facts_used"] = rev.payload["facts_used"]
                return str(rev.payload.get("body") or "")

            revised_body, msg_verdict, blocked_by_rule = await self._run_qa_gate(
                channel=raw_channel,
                html_or_body=body,
                is_landing_page=False,
                revise_fn=_revise_msg,
                expected_url=registered_url,
                render_prefix=f"{session_id}_t{touch_spec.n}",
            )
            if blocked_by_rule:
                reason = (
                    "; ".join(msg_verdict.required_fixes)
                    if msg_verdict and msg_verdict.required_fixes
                    else "email failed rule compliance QA"
                )
                self._consecutive_rejections += 1
                actions.append(
                    _action("reject", proposal_id=msg_prop.proposal_id, reason=reason)
                )
                if self._consecutive_rejections >= 3:
                    await self._close_active_conversations()
                    self.state.transition_state(
                        "ABORT",
                        reason=f"3 consecutive rule-blocked messages: {reason}",
                    )
                self.state.log_decision(
                    DecisionRecord(
                        trigger="agent_output",
                        snapshot_version=self.snapshot.version,
                        actions=actions,
                        notes=f"QA blocked outbound email for rule compliance: {reason}",
                    )
                )
                return self.snapshot
            msg_prop.payload["body"] = revised_body
            for meta_key in ("subject", "attachments", "facts_used"):
                if meta_key in revised_meta:
                    msg_prop.payload[meta_key] = revised_meta[meta_key]
            msg_prop.base_version = self.snapshot.version

        # Validate message proposal before spending a touch (retry compose once with feedback)
        known_pages = {p.url for p in self.state.store.get_pages_for_session(session_id)}
        ok, reject_reason = self.state.validate_message_proposal(
            msg_prop,
            registered_page_urls=known_pages,
        )
        if not ok:
            actions.append(
                _action("reject", proposal_id=msg_prop.proposal_id, reason=reject_reason)
            )
            retry_prop = await self._safe_run_agent(
                "messaging",
                self.agents.messaging,
                mode=compose_mode,
                channel=touch_spec.channel,
                touch=touch_spec.model_dump(mode="json"),
                registered_page_url=registered_url,
                latest_signal=self._last_signal_payload,
                rejection_feedback=reject_reason,
            )
            if retry_prop is not None:
                msg_prop = retry_prop
                raw_channel = str(msg_prop.payload.get("channel") or touch_spec.channel)
                ok, reject_reason = self.state.validate_message_proposal(
                    msg_prop,
                    registered_page_urls=known_pages,
                )

        if not ok:
            self._consecutive_rejections += 1
            actions.append(
                _action("reject", proposal_id=msg_prop.proposal_id, reason=reject_reason)
            )
            if self._consecutive_rejections >= 3:
                await self._close_active_conversations()
                self.state.transition_state(
                    "ABORT",
                    reason=f"3 consecutive invalid message proposals: {reject_reason}",
                )
            self.state.log_decision(
                DecisionRecord(
                    trigger="agent_output",
                    snapshot_version=self.snapshot.version,
                    actions=actions,
                    notes=f"Rejected outbound message proposal: {reject_reason}",
                )
            )
            return self.snapshot

        self._consecutive_rejections = 0
        channel: Channel = raw_channel if raw_channel in VALID_CHANNELS else "email"  # type: ignore[assignment]
        body = str(msg_prop.payload.get("say") or msg_prop.payload["body"])
        subject_raw = msg_prop.payload.get("subject")
        subject = str(subject_raw) if subject_raw is not None else None
        attachments = msg_prop.payload.get("attachments") or None
        sent_at_raw = msg_prop.payload.get("sent_at")
        sent_at = str(sent_at_raw) if sent_at_raw else None
        sender_obj = self._sender_dict(channel=channel, msg_payload=msg_prop.payload)
        facts_used = [str(f) for f in msg_prop.payload.get("facts_used", [])]
        page_request = msg_prop.payload.get("page_request")
        required_page_url = (
            self._build_landing_page_url(str(page_request)) if page_request else None
        )
        if (
            required_page_url is None
            and self.snapshot.campaign.plan is not None
            and self.snapshot.campaign.plan.landing_page is not None
        ):
            planned_url = self._build_landing_page_url(self.snapshot.campaign.plan.landing_page.url_path)
            if planned_url in body:
                required_page_url = planned_url
        idempotency_key = str(uuid4())

        # Execute send via Target API
        api_t0 = time.monotonic()
        api_step_id = uuid4().hex[:8]
        self.state.record_stage_start("target_api", mode=channel, step_id=api_step_id)
        try:
            if self.policy.is_live_channel(channel):
                cid = self._active_conversations.get(channel)
                if cid is None:
                    conv_res = await self.api.start_conversation(
                        session_id,
                        to=self.snapshot.target.id,
                        channel=channel,
                        sender=sender_obj,
                        sent_at=sent_at,
                        idempotency_key=idempotency_key,
                    )
                    cid = str(conv_res.get("conversation_id") or conv_res.get("id") or "")
                    if not cid:
                        raise TargetAPIError(500, f"start_conversation missing id: {conv_res}")
                    self._active_conversations[channel] = cid
                api_res = await self.api.send_conversation_turn(
                    cid,
                    body,
                    attachments=attachments,
                    sent_at=sent_at,
                    idempotency_key=str(uuid4()),
                )
            else:
                api_res = await self.api.send_message(
                    session_id,
                    to=self.snapshot.target.id,
                    channel=channel,
                    body=body,
                    sender=sender_obj,
                    subject=subject,
                    attachments=attachments,
                    sent_at=sent_at,
                    required_page_url=required_page_url,
                    idempotency_key=idempotency_key,
                )
            self.state.record_stage_timing(
                "target_api",
                elapsed_ms=int((time.monotonic() - api_t0) * 1000),
                ok=True,
                mode=channel,
                step_id=api_step_id,
            )
        except BudgetExhaustedError as exc:
            self.state.record_stage_timing(
                "target_api",
                elapsed_ms=int((time.monotonic() - api_t0) * 1000),
                ok=False,
                mode=channel,
                step_id=api_step_id,
            )
            self.state.record_system_error("api", str(exc))
            self.state.transition_state("DONE", reason="API returned 409 budget/session exhausted")
            self.state.log_decision(
                DecisionRecord(
                    trigger="target_event",
                    snapshot_version=self.snapshot.version,
                    actions=actions,
                    notes=f"Stopped on 409 from Target API: {exc}",
                )
            )
            return self.snapshot
        except SimulatorRejectionError as exc:
            self.state.record_stage_timing(
                "target_api",
                elapsed_ms=int((time.monotonic() - api_t0) * 1000),
                ok=False,
                mode=channel,
                step_id=api_step_id,
            )
            self.state.record_system_error("api", str(exc))
            actions.append(
                _action(
                    "trigger",
                    agent="strategy",
                    reason=f"Target API 422 rejection: {exc.detail}",
                )
            )
            replan = await self._safe_run_agent(
                "strategy",
                self.agents.strategy,
                mode="pivot",
                reason=f"422 rejection on channel {channel}: {exc.detail}",
            )
            if replan is not None:
                actions.extend(self._actions_from_commits([self.state.commit_proposal(replan)]))
            self.state.log_decision(
                DecisionRecord(
                    trigger="target_event",
                    snapshot_version=self.snapshot.version,
                    actions=actions,
                    notes=f"Handled 422 rejection without blind retry: {exc.detail}",
                )
            )
            return self.snapshot
        except (PageNotRegisteredError, TargetAPIError) as exc:
            self.state.record_stage_timing(
                "target_api",
                elapsed_ms=int((time.monotonic() - api_t0) * 1000),
                ok=False,
                mode=channel,
                step_id=api_step_id,
            )
            self.state.record_system_error("api", str(exc))
            self.state.log_decision(
                DecisionRecord(
                    trigger="target_event",
                    snapshot_version=self.snapshot.version,
                    actions=actions,
                    notes=f"API error during send: {exc}",
                )
            )
            return self.snapshot

        # Record outbound touch & returned target events
        self.state.record_outbound_turn(
            channel=channel,
            body=body,
            subject=subject,
            idempotency_key=idempotency_key,
            facts_used=facts_used,
            attachments=attachments,
            cost_touch=True,
        )
        actions.append(
            _action(
                "send",
                proposal_id=msg_prop.proposal_id,
                channel=channel,
                idempotency_key=idempotency_key,
                touch_n=self.snapshot.campaign.budget.used,
            )
        )

        raw_events = api_res.get("events", [])
        if not isinstance(raw_events, list):
            raw_events = []
        recorded_events: list[Event] = [
            self.state.record_target_event(ev, default_channel=channel)
            for ev in raw_events
            if isinstance(ev, dict)
        ]

        had_reply = any(e.payload.get("event") == "reply" for e in recorded_events)
        self.policy.record_touch_sent(
            channel,
            had_reply=had_reply,
            event_count=len(recorded_events),
        )

        # Immediate win detection (credentials / action)
        registered_pages = self.state.store.get_pages_for_session(session_id)
        win = evaluate_wins(
            recorded_events,
            plan=self.snapshot.campaign.plan,
            registered_pages=registered_pages,
        )
        if win is not None:
            self.state.record_incident(
                objective=win.objective,
                evidence_event_ids=win.evidence_event_ids,
                summary=win.summary,
                details=win.details,
            )
            actions.append(
                _action(
                    "declare_incident",
                    objective=win.objective,
                    evidence=win.evidence_event_ids,
                    summary=win.summary,
                )
            )
            self.state.log_decision(
                DecisionRecord(
                    trigger="target_event",
                    snapshot_version=self.snapshot.version,
                    actions=actions,
                    notes=f"Objective achieved ({win.objective}): {win.summary}",
                )
            )
            return self.snapshot

        # Signal extraction on replies
        for ev in recorded_events:
            if ev.payload.get("event") == "reply":
                sig_prop = await self._safe_run_agent(
                    "messaging",
                    self.agents.messaging,
                    mode="signal",
                    event=ev.model_dump(mode="json"),
                )
                if sig_prop is not None:
                    self.state.commit_proposal(sig_prop)
                    self._last_signal_payload = dict(sig_prop.payload)
                    engagement = float(sig_prop.payload.get("engagement", 0.5))
                    suspicion = float(sig_prop.payload.get("suspicion", 0.0))
                    self.policy.record_signal(engagement=engagement, suspicion=suspicion)

        # Profile update (batched every >=2 target events, or on every reply for opportunistic disclosure wins)
        disclosure_candidates: list[dict[str, Any]] | None = None
        should_profile = self.policy.should_trigger_profile() or had_reply
        if should_profile and self.agents.profile is not None:
            actions.append(_action("trigger", agent="profile", reason="target event batch"))
            prof_prop = await self._safe_run_agent(
                "profile",
                self.agents.profile,
                mode="update",
                recent_events=[e.model_dump(mode="json") for e in recorded_events],
                recent_signals=(
                    [self._last_signal_payload] if self._last_signal_payload is not None else None
                ),
            )
            if prof_prop is not None:
                actions.extend(self._actions_from_commits([self.state.commit_proposal(prof_prop)]))
                self.policy.mark_profile_ran()
                raw_disc = prof_prop.payload.get("disclosure_candidates") or prof_prop.payload.get(
                    "disclosures"
                )
                if isinstance(raw_disc, list):
                    disclosure_candidates = [d for d in raw_disc if isinstance(d, dict)]

        # Check disclosure win
        disc_win = evaluate_wins(
            [],
            plan=self.snapshot.campaign.plan,
            registered_pages=registered_pages,
            disclosure_candidates=disclosure_candidates,
        )
        if disc_win is not None:
            self.state.record_incident(
                objective=disc_win.objective,
                evidence_event_ids=disc_win.evidence_event_ids,
                summary=disc_win.summary,
                details=disc_win.details,
            )
            actions.append(
                _action(
                    "declare_incident",
                    objective=disc_win.objective,
                    evidence=disc_win.evidence_event_ids,
                    summary=disc_win.summary,
                )
            )
            self.state.log_decision(
                DecisionRecord(
                    trigger="target_event",
                    snapshot_version=self.snapshot.version,
                    actions=actions,
                    notes=f"Disclosure objective achieved: {disc_win.summary}",
                )
            )
            return self.snapshot

        # Optional capped OSINT refresh
        if self.agents.osint is not None and self.policy.should_trigger_osint_refresh(self.snapshot):
            actions.append(
                _action(
                    "trigger",
                    agent="osint",
                    reason=f"profile confidence {self.snapshot.profile.overall_confidence:.2f} < 0.50",
                )
            )
            osint_prop = await self._safe_run_agent("osint", self.agents.osint, mode="refresh")
            self.policy.mark_osint_refreshed()
            if osint_prop is not None:
                actions.extend(self._actions_from_commits([self.state.commit_proposal(osint_prop)]))

        # Strategy pivot check
        pivot_reason = self.policy.strategy_pivot_reason(self.snapshot)
        if pivot_reason is not None and self.agents.strategy is not None:
            actions.append(_action("trigger", agent="strategy", reason=pivot_reason))
            strat_prop = await self._safe_run_agent(
                "strategy",
                self.agents.strategy,
                mode="pivot",
                reason=pivot_reason,
            )
            self.policy.mark_strategy_pivoted()
            if strat_prop is not None:
                actions.extend(self._actions_from_commits([self.state.commit_proposal(strat_prop)]))
                await self._maybe_restart_session(actions)

        # Final budget check
        if not self.policy.can_spend_touch(self.snapshot):
            self.state.transition_state("DONE", reason="session touch budget reached")

        self.state.log_decision(
            DecisionRecord(
                trigger="target_event" if recorded_events else "pacing",
                snapshot_version=self.snapshot.version,
                actions=actions,
                notes=(
                    f"Touch {self.snapshot.campaign.budget.used}/"
                    f"{self.snapshot.campaign.budget.session_max} via {channel}; "
                    f"events={len(recorded_events)}."
                ),
            )
        )
        return self.snapshot

    async def run(self, *, max_cycles: int = 50) -> Snapshot:
        exit_reason = "unknown"
        try:
            if self.snapshot.run.state == "BOOT":
                await self.bootstrap()

            cycles = 0
            while self.snapshot.run.state not in {"WIN", "ABORT", "DONE"} and cycles < max_cycles:
                await self.step()
                cycles += 1

            if self.snapshot.run.state in {"WIN", "DONE", "ABORT"}:
                exit_reason = "completed"
            elif cycles >= max_cycles:
                exit_reason = "cycle_limit"
        except Exception:
            exit_reason = "service_failure"
            raise
        finally:
            await self._close_active_conversations()
            self.state.record_run_exit(exit_reason)
        return self.snapshot
