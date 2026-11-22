from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from src.agents.messaging import MAX_CONVERSATION_TURNS, StoryClock
from src.agents.visual_artifacts import render_visual_artifact_png
from src.api.client import (
    AssetLimitError,
    BudgetExhaustedError,
    ConversationEndedError,
    PageNotRegisteredError,
    SimulatorRejectionError,
    StoryTimeError,
    TargetAPIClient,
    TargetAPIError,
)
from src.config import AppConfig
from src.models import (
    Channel,
    DecisionAction,
    DecisionRecord,
    Event,
    LandingPageSpec,
    Proposal,
    QAVerdict,
    Snapshot,
    TargetInfo,
    TouchSpec,
)
from src.orchestrator.policy import TriggerPolicy
from src.orchestrator.state import VALID_CHANNELS, CommitResult, StateManager
from src.orchestrator.wins import (
    classify_page_submission,
    evaluate_wins,
    extract_page_submission_summary,
)
from src.store.memory import TargetMemory, TargetMemoryStore

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
        memory_store: TargetMemoryStore | None = None,
        story_clock: StoryClock | None = None,
        renderer: Any | None = None,
        max_sessions_override: int | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.api = api
        self.agents = agents
        self.aggressive = aggressive
        self._sleep = sleep
        self.memory_store = memory_store or TargetMemoryStore(state.store.db_path.parent)
        self.story_clock = story_clock or StoryClock()
        self._renderer = renderer
        self.max_sessions_override = max_sessions_override

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
        self._channels_catalog: list[dict[str, Any]] = []
        self._channels_by_id: dict[str, dict[str, Any]] = {}
        self._target_memory: TargetMemory | None = None
        self._visual_proof_asset_id: str | None = None
        self._campaign_memory: dict[str, Any] = {
            "registered_urls": [],
            "mentioned_urls": [],
            "mentioned_dates": [],
            "subjects": [],
        }
        self._funnel_state: dict[str, Any] = {
            "links_sent": [],
            "links_opened": [],
            "step_up_submitted": [],
        }

    def _sync_story_clock(self, iso_ts: str | None) -> None:
        """Advance the monotonic StoryClock forward if a remote story_time is ahead."""
        if not iso_ts:
            return
        try:
            remote_dt = datetime.fromisoformat(iso_ts)
        except ValueError:
            return
        if remote_dt.tzinfo is None:
            remote_dt = remote_dt.replace(tzinfo=self.story_clock.tz)
        else:
            remote_dt = remote_dt.astimezone(self.story_clock.tz)
        current_dt = datetime.fromisoformat(self.story_clock.current_iso)
        if remote_dt >= current_dt:
            self.story_clock._current = (remote_dt + timedelta(seconds=1)).replace(microsecond=0)

    async def _recover_story_time(self, session_id: str) -> str:
        """Query remote session story_time after a 409 StoryTimeError and advance monotonically."""
        try:
            remote = await self.api.get_session(session_id)
            remote_st = remote.get("story_time")
            if isinstance(remote_st, str) and remote_st:
                self._sync_story_clock(remote_st)
        except TargetAPIError:
            pass
        return self.story_clock.advance_message(minutes=5)

    async def _load_channels_catalog(self) -> None:
        if self._channels_catalog:
            return
        try:
            channels = await self.api.get_channels()
        except TargetAPIError:
            return
        self._channels_catalog = channels
        self._channels_by_id = {
            str(ch.get("id") or ch.get("channel")): ch
            for ch in channels
            if isinstance(ch, dict) and (ch.get("id") or ch.get("channel"))
        }

    def _build_win_goal_status(
        self,
        *,
        registered_url: str | None = None,
        session_registered_urls: list[str] | None = None,
    ) -> dict[str, Any]:
        urls = list(session_registered_urls or [])
        if registered_url and registered_url not in urls:
            urls.append(registered_url)
        active_url = registered_url or (urls[-1] if urls else "")
        links_opened = list(self._funnel_state["links_opened"])
        step_up = list(self._funnel_state["step_up_submitted"])
        links_sent = list(self._funnel_state["links_sent"])
        remaining = max(
            0, self.snapshot.campaign.budget.session_max - self.snapshot.campaign.budget.used
        )

        if step_up:
            stage = "step_up_submitted"
            directive = (
                f"Step-up form submitted; route directly to {active_url or 'the portal URL'} "
                "for final form submission (page.submitted)."
            )
        elif links_opened:
            stage = "link_opened_not_submitted"
            opened_u = links_opened[-1] or active_url
            directive = (
                f"Target already opened {opened_u} (link.opened). Resolve any hesitation "
                f"and guide them to complete and submit the form on {active_url or opened_u}."
            )
        elif links_sent:
            stage = "link_sent_unopened"
            directive = (
                f"Portal URL {active_url or links_sent[-1]} was already sent but not submitted. "
                "Answer their question concisely and direct them to open and submit the form."
            )
        elif active_url:
            stage = "ready_to_send_link"
            directive = (
                f"Include {active_url} naturally with a clear, concrete reason to sign in "
                "and submit the portal form."
            )
        else:
            stage = "link_not_sent_yet"
            directive = (
                "Build trust quickly and set up handoff to the registered portal page "
                "to achieve page.submitted."
            )

        return {
            "target_event": "page.submitted",
            "required_objective": "credentials",
            "stage": stage,
            "active_url": active_url or None,
            "registered_urls": urls,
            "links_sent": links_sent,
            "links_opened": links_opened,
            "step_up_submitted": step_up,
            "touches_remaining": remaining,
            "directive": directive,
        }

    def _strategy_context_kwargs(self) -> dict[str, Any]:
        session_id = self.snapshot.sessions.current
        reg_urls = (
            [p.url for p in self.state.store.get_pages_for_session(session_id)]
            if session_id
            else []
        )
        kwargs: dict[str, Any] = {
            "current_story_time": self.story_clock.current_iso,
            "win_goal_status": self._build_win_goal_status(session_registered_urls=reg_urls),
        }
        if self._channels_catalog:
            kwargs["available_channels"] = self._channels_catalog
        if self._target_memory is not None and self._target_memory.sessions_seen > 0:
            kwargs["target_memory"] = self._target_memory.to_strategy_context()
        return kwargs

    async def _reconcile_resumed_session(self) -> None:
        session_id = self.snapshot.sessions.current
        if not session_id:
            return
        try:
            remote = await self.api.get_session(session_id)
        except TargetAPIError:
            return
        remote_st = remote.get("story_time")
        if isinstance(remote_st, str) and remote_st:
            self._sync_story_clock(remote_st)
        remote_events: list[dict[str, Any]] = []
        try:
            remote_events = await self.api.get_session_events(session_id)
        except TargetAPIError:
            pass
        self.state.reconcile_remote_session(remote, remote_events)

    def _record_campaign_memory(
        self,
        *,
        body: str = "",
        subject: str | None = None,
        registered_url: str | None = None,
    ) -> None:
        """Freeze key dates, URLs, and subjects across sessions so restarts never contradict."""
        if registered_url and registered_url not in self._campaign_memory["registered_urls"]:
            self._campaign_memory["registered_urls"].append(registered_url)
        if subject and subject.strip():
            clean_s = re.sub(r"^(?:re:\s*)+", "", subject.strip(), flags=re.IGNORECASE)
            if clean_s and clean_s not in self._campaign_memory["subjects"]:
                self._campaign_memory["subjects"].append(clean_s)
                self._campaign_memory["subjects"] = self._campaign_memory["subjects"][-6:]
        if body:
            for m in re.finditer(r"https?://[^\s<>\"')]+", body, re.IGNORECASE):
                u = m.group(0).rstrip(".,;:!?")
                if u not in self._campaign_memory["mentioned_urls"]:
                    self._campaign_memory["mentioned_urls"].append(u)
                    self._campaign_memory["mentioned_urls"] = self._campaign_memory["mentioned_urls"][-8:]
            for m in re.finditer(
                r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
                r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
                r"Dec(?:ember)?)\s+\d{1,2}(?:,\s*20\d{2})?\b|\bQ[1-4]\s*20\d{2}\b",
                body,
                re.IGNORECASE,
            ):
                d_str = m.group(0).strip()
                if d_str not in self._campaign_memory["mentioned_dates"]:
                    self._campaign_memory["mentioned_dates"].append(d_str)
                    self._campaign_memory["mentioned_dates"] = self._campaign_memory["mentioned_dates"][-6:]

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
        if self.max_sessions_override is not None:
            default_sessions = min(default_sessions, max(1, self.max_sessions_override))
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
            if self.max_sessions_override is not None:
                max_sessions = min(max_sessions, max(1, self.max_sessions_override))
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
            await self._load_channels_catalog()
            self._target_memory = self.memory_store.load(self.config.run.scenario, target.id)
            if self._target_memory.sessions_seen > 0:
                self.state.seed_from_memory(self._target_memory)
            sess_res = await self.api.create_session(self.config.run.scenario)
            session_id = str(sess_res.get("session_id") or sess_res.get("id") or "")
            if not session_id:
                raise TargetAPIError(500, f"create_session response missing session_id: {sess_res}")
            remote_st = sess_res.get("story_time")
            if isinstance(remote_st, str) and remote_st:
                self._sync_story_clock(remote_st)
            self.state.start_session(session_id, session_max_touches=touch_budget)
            self.state.transition_state("RESEARCH", reason="session created; starting bootstrap")
        else:
            await self._load_channels_catalog()
            if self._target_memory is None and self.snapshot.target.id:
                self._target_memory = self.memory_store.load(
                    self.snapshot.run.scenario, self.snapshot.target.id
                )

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
        strategy_prop = await self._safe_run_agent(
            "strategy",
            self.agents.strategy,
            mode="plan",
            **self._strategy_context_kwargs(),
        )
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

    def _should_defer_live_call_to_email(self) -> bool:
        """Avoid burning a live call when the target just asked for written details/email."""
        inbound = [t for t in self.snapshot.conversation.turns if t.dir == "in"]
        if not inbound:
            return False
        last_body = (inbound[-1].body or "").lower()
        email_request_cues = (
            "by mail",
            "by email",
            "over email",
            "send me",
            "send it over",
            "send those over",
            "one-pager",
            "once my calendar settles",
            "write you by mail",
            "come back to you once",
            "get back to you once",
        )
        return any(cue in last_body for cue in email_request_cues)

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
        email_style: str | None = None,
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
                email_style=email_style,
                revision=revision,
                revision_cycle=revision,
                asset_map=asset_map or None,
                page_url=page_url,
                expected_url=expected_url,
                landing_page_spec=landing_page_spec,
                reference_date=self.story_clock.current_iso[:10],
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

    async def _register_single_page(
        self,
        spec: LandingPageSpec,
        *,
        session_id: str,
        next_touch_n: int,
        actions: list[DecisionAction],
        force: bool = False,
    ) -> str | None:
        page_url = self._build_landing_page_url(spec.url_path)
        if not force and self.api.is_page_registered(session_id, page_url):
            return page_url

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
            force=force,
        )
        self._record_campaign_memory(registered_url=page_url)
        actions.append(_action("register_page", url=page_url, title=title, fields=fields))
        return page_url

    async def _ensure_landing_page_registered(
        self,
        next_touch_n: int,
        actions: list[DecisionAction],
    ) -> str | None:
        plan = self.snapshot.campaign.plan
        session_id = self.snapshot.sessions.current
        if plan is None or not session_id:
            return None

        specs: list[LandingPageSpec] = []
        if plan.landing_page is not None:
            specs.append(plan.landing_page)
        specs.extend(plan.pages)
        if not specs:
            return None

        last_in = next(
            (t for t in reversed(self.snapshot.conversation.turns) if t.dir == "in"),
            None,
        )
        asked_for_url = bool(
            last_in is not None
            and re.search(
                r"\b(?:url|link|website|site|web\s*page|event\s+page|landing\s+page|registration\s+page|portal|one-?pager|deck|overview)\b",
                last_in.body,
                re.IGNORECASE,
            )
        )

        existing_by_url = {
            p.url: p for p in self.state.store.get_pages_for_session(session_id)
        }
        chosen_url: str | None = None
        for spec in specs:
            url = self._build_landing_page_url(spec.url_path)
            existing = existing_by_url.get(url)
            fields_changed = (
                existing is not None
                and bool(spec.fields)
                and {f.lower() for f in spec.fields} != {f.lower() for f in existing.fields}
            )

            if existing is not None and not fields_changed:
                if next_touch_n >= spec.trigger_touch or asked_for_url:
                    chosen_url = url
                continue

            if next_touch_n < spec.trigger_touch and not asked_for_url:
                continue

            registered = await self._register_single_page(
                spec,
                session_id=session_id,
                next_touch_n=next_touch_n,
                actions=actions,
                force=fields_changed,
            )
            if registered:
                chosen_url = registered

        return chosen_url

    async def _maybe_upload_visual_proof(self, touch_spec: TouchSpec) -> str | None:
        """Render and upload an on-demand PNG briefing sheet when renderer is available."""
        if (
            self._renderer is None
            or self._visual_proof_asset_id is not None
            or self.snapshot.identity is None
            or touch_spec.escalation < 2
            or touch_spec.channel not in {"email", "shared_document", "linkedin_message"}
        ):
            return self._visual_proof_asset_id
        plan = self.snapshot.campaign.plan
        if plan is None or plan.objective not in {"credentials", "disclosure"}:
            return None
        bullets = [h.topic for h in self.snapshot.hooks if h.status != "exhausted"][:3]
        if not bullets:
            return None
        out_path = self.config.run.data_dir / "renders" / f"{self.snapshot.target.id}_brief.png"
        try:
            await render_visual_artifact_png(
                self._renderer,
                title=self.snapshot.identity.company or "Program Brief",
                subtitle=touch_spec.intent,
                bullets=bullets,
                manifest=self.snapshot.identity,
                out_path=out_path,
            )
            record = await self.api.upload_asset(out_path)
            self._visual_proof_asset_id = record.asset_id
            return record.asset_id
        except Exception:  # noqa: BLE001
            return None

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
        self.story_clock = StoryClock()
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
            campaign_memory=dict(self._campaign_memory),
            **self._strategy_context_kwargs(),
        )
        if replan is not None:
            actions.extend(self._actions_from_commits([self.state.commit_proposal(replan)]))
        return True

    async def _execute_live_conversation(
        self,
        *,
        session_id: str,
        channel: Channel,
        touch_spec: TouchSpec,
        initial_msg_prop: Proposal,
        initial_body: str,
        initial_attachments: list[str | dict[str, Any]] | None,
        initial_facts_used: list[str],
        sender_obj: dict[str, str] | None,
        sent_at: str,
        registered_url: str | None,
        session_registered_urls: list[str],
        actions: list[DecisionAction],
    ) -> tuple[list[Event], list[dict[str, Any]], bool]:
        """Run an interactive multi-turn live conversation (1 touch for up to 10 turns)."""
        recorded_events: list[Event] = []
        call_transcript: list[dict[str, Any]] = []
        idempotency_key = str(uuid4())

        # Start conversation (bills 1 touch on Target API)
        start_sent_at = sent_at
        try:
            conv_res = await self.api.start_conversation(
                session_id,
                to=self.snapshot.target.id,
                channel=channel,
                sender=sender_obj,
                sent_at=start_sent_at,
                idempotency_key=idempotency_key,
            )
        except StoryTimeError:
            start_sent_at = await self._recover_story_time(session_id)
            conv_res = await self.api.start_conversation(
                session_id,
                to=self.snapshot.target.id,
                channel=channel,
                sender=sender_obj,
                sent_at=start_sent_at,
                idempotency_key=str(uuid4()),
            )

        cid = str(conv_res.get("conversation_id") or conv_res.get("id") or "")
        if not cid:
            raise TargetAPIError(500, f"start_conversation missing id: {conv_res}")
        self._active_conversations[channel] = cid

        # Record opening persona greeting if returned
        call_start_story_time = self.story_clock.current_iso
        last_persona_says = str(conv_res.get("persona_says") or "").strip()
        if last_persona_says:
            greeting_ev = self.state.record_target_event(
                {
                    "type": "persona_says",
                    "channel": channel,
                    "body": last_persona_says,
                    "conversation_id": cid,
                    "turn": 0,
                },
                default_channel=channel,
                story_time=call_start_story_time,
            )
            recorded_events.append(greeting_ev)
            call_transcript.append({"speaker": "target", "turn": 0, "text": last_persona_says})

        for raw_ev in conv_res.get("events", []) or []:
            if isinstance(raw_ev, dict):
                recorded_events.append(
                    self.state.record_target_event(
                        raw_ev, default_channel=channel, story_time=call_start_story_time
                    )
                )

        touch_billed = False
        registered_pages = self.state.store.get_pages_for_session(session_id)

        try:
            for turn_idx in range(1, MAX_CONVERSATION_TURNS + 1):
                turn_sent_at = self.story_clock.advance_turn(seconds=25)
                if turn_idx == 1:
                    say_text = initial_body
                    turn_attachments = initial_attachments
                    turn_facts = initial_facts_used
                    hang_up = bool(initial_msg_prop.payload.get("hang_up", False))
                    prop_id = initial_msg_prop.proposal_id
                else:
                    turn_prop = await self._safe_run_agent(
                        "messaging",
                        self.agents.messaging,
                        mode="live_turn",
                        channel=channel,
                        touch=touch_spec.model_dump(mode="json"),
                        persona_says=last_persona_says,
                        turn_index=turn_idx,
                        current_story_time=turn_sent_at,
                        sent_at=turn_sent_at,
                        registered_page_url=registered_url,
                        known_registered_urls=session_registered_urls,
                        channel_info=self._channels_by_id.get(channel),
                        sender_alias=touch_spec.sender_alias,
                        win_goal_status=self._build_win_goal_status(
                            registered_url=registered_url,
                            session_registered_urls=session_registered_urls,
                        ),
                        campaign_memory=dict(self._campaign_memory),
                        latest_signal=self._last_signal_payload,
                    )
                    if turn_prop is None:
                        break
                    say_text = str(
                        turn_prop.payload.get("say") or turn_prop.payload.get("body") or ""
                    ).strip()
                    if not say_text:
                        break
                    turn_attachments = turn_prop.payload.get("attachments") or None
                    turn_facts = [str(f) for f in turn_prop.payload.get("facts_used", [])]
                    hang_up = bool(turn_prop.payload.get("hang_up", False))
                    prop_id = turn_prop.proposal_id

                try:
                    turn_res = await self.api.send_conversation_turn(
                        cid,
                        say_text,
                        attachments=turn_attachments,
                        sent_at=turn_sent_at,
                        idempotency_key=str(uuid4()),
                    )
                except StoryTimeError:
                    turn_sent_at = await self._recover_story_time(session_id)
                    turn_res = await self.api.send_conversation_turn(
                        cid,
                        say_text,
                        attachments=turn_attachments,
                        sent_at=turn_sent_at,
                        idempotency_key=str(uuid4()),
                    )
                except ConversationEndedError:
                    break

                self.state.record_outbound_turn(
                    channel=channel,
                    body=say_text,
                    subject=None,
                    idempotency_key=idempotency_key if not touch_billed else str(uuid4()),
                    facts_used=turn_facts,
                    attachments=turn_attachments,
                    cost_touch=not touch_billed,
                    story_time=turn_sent_at,
                )
                if not touch_billed:
                    touch_billed = True
                    actions.append(
                        _action(
                            "send",
                            proposal_id=prop_id,
                            channel=channel,
                            idempotency_key=idempotency_key,
                            touch_n=self.snapshot.campaign.budget.used,
                            conversation_id=cid,
                        )
                    )
                call_transcript.append({"speaker": "sender", "turn": turn_idx, "text": say_text})

                reply_text = str(turn_res.get("persona_says") or "").strip()
                if reply_text:
                    last_persona_says = reply_text
                    reply_ev = self.state.record_target_event(
                        {
                            "type": "persona_says",
                            "channel": channel,
                            "body": reply_text,
                            "conversation_id": cid,
                            "turn": turn_idx,
                        },
                        default_channel=channel,
                        story_time=turn_sent_at,
                    )
                    recorded_events.append(reply_ev)
                    call_transcript.append({"speaker": "target", "turn": turn_idx, "text": reply_text})

                turn_events = turn_res.get("events", [])
                if isinstance(turn_events, list):
                    for raw_ev in turn_events:
                        if isinstance(raw_ev, dict):
                            recorded_events.append(
                                self.state.record_target_event(
                                    raw_ev, default_channel=channel, story_time=turn_sent_at
                                )
                            )

                # Immediate win check on events emitted during this call turn
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
                            notes=f"Objective achieved during live {channel} ({win.objective}): {win.summary}",
                        )
                    )
                    return recorded_events, call_transcript, True

                if bool(turn_res.get("ended")) or hang_up:
                    break
        finally:
            if not touch_billed:
                # start_conversation consumed 1 touch even if turn 1 failed immediately
                self.snapshot.campaign.budget.used += 1
                self.state.store.save_snapshot(self.snapshot)
            try:
                await self.api.end_conversation(cid)
            except TargetAPIError:
                pass
            if self._active_conversations.get(channel) == cid:
                self._active_conversations.pop(channel, None)

        return recorded_events, call_transcript, False

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
                campaign_memory=dict(self._campaign_memory),
                **self._strategy_context_kwargs(),
            )
            if plan_prop is not None:
                actions.extend(self._actions_from_commits([self.state.commit_proposal(plan_prop)]))

        touch_spec = self._next_touch_spec()
        if self.policy.is_live_channel(touch_spec.channel) and self._should_defer_live_call_to_email():
            touch_spec = touch_spec.model_copy(
                update={
                    "channel": "email",
                    "email_style": "personal_html",
                    "intent": (
                        f"Deliver requested written details by email first, then advance objective ({touch_spec.intent})"
                    ),
                }
            )
        registered_url = await self._ensure_landing_page_registered(touch_spec.n, actions)
        session_registered_urls = [p.url for p in self.state.store.get_pages_for_session(session_id)]
        await self._maybe_upload_visual_proof(touch_spec)

        # Advance monotonic story time for this contact
        planned_delay = touch_spec.story_delay_minutes if touch_spec.story_delay_minutes else 95
        contact_sent_at = self.story_clock.advance_message(minutes=planned_delay)
        active_plan = self.snapshot.campaign.plan
        channel_info = self._channels_by_id.get(touch_spec.channel)

        # Compose message (use live_turn mode for live voice/chat channels)
        win_goal_status = self._build_win_goal_status(
            registered_url=registered_url,
            session_registered_urls=session_registered_urls,
        )
        compose_mode = "live_turn" if self.policy.is_live_channel(touch_spec.channel) else "compose"
        msg_prop = await self._safe_run_agent(
            "messaging",
            self.agents.messaging,
            mode=compose_mode,
            channel=touch_spec.channel,
            touch=touch_spec.model_dump(mode="json"),
            registered_page_url=registered_url,
            known_registered_urls=session_registered_urls,
            campaign_memory=dict(self._campaign_memory),
            latest_signal=self._last_signal_payload,
            sent_at=contact_sent_at,
            current_story_time=contact_sent_at,
            channel_info=channel_info,
            email_style=touch_spec.email_style,
            sender_alias=touch_spec.sender_alias,
            win_goal_status=win_goal_status,
            action_artifact=active_plan.action_artifact if active_plan else None,
            objective=active_plan.objective if active_plan else None,
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
                    known_registered_urls=session_registered_urls,
                    campaign_memory=dict(self._campaign_memory),
                    latest_signal=self._last_signal_payload,
                    sent_at=contact_sent_at,
                    current_story_time=contact_sent_at,
                    channel_info=channel_info,
                    email_style=touch_spec.email_style,
                    sender_alias=touch_spec.sender_alias,
                    win_goal_status=win_goal_status,
                    action_artifact=active_plan.action_artifact if active_plan else None,
                    objective=active_plan.objective if active_plan else None,
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
                email_style=str(msg_prop.payload.get("email_style") or touch_spec.email_style or "")
                or None,
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
                known_registered_urls=session_registered_urls,
                sent_at=contact_sent_at,
                current_story_time=contact_sent_at,
                channel_info=channel_info,
                email_style=touch_spec.email_style,
                sender_alias=touch_spec.sender_alias,
                win_goal_status=win_goal_status,
                action_artifact=active_plan.action_artifact if active_plan else None,
                objective=active_plan.objective if active_plan else None,
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
        self._record_campaign_memory(body=body, subject=subject)
        attachments = msg_prop.payload.get("attachments") or None
        sent_at_raw = msg_prop.payload.get("sent_at")
        sent_at = str(sent_at_raw) if sent_at_raw else contact_sent_at
        sender_obj = self._sender_dict(channel=channel, msg_payload=msg_prop.payload)
        facts_used = [str(f) for f in msg_prop.payload.get("facts_used", [])]
        page_request = msg_prop.payload.get("page_request")
        required_page_url = (
            self._build_landing_page_url(str(page_request)) if page_request else None
        )
        if required_page_url is None and self.snapshot.campaign.plan is not None:
            candidate_specs = [
                *(
                    [self.snapshot.campaign.plan.landing_page]
                    if self.snapshot.campaign.plan.landing_page is not None
                    else []
                ),
                *self.snapshot.campaign.plan.pages,
            ]
            for page_spec in candidate_specs:
                planned_url = self._build_landing_page_url(page_spec.url_path)
                if planned_url in body:
                    required_page_url = planned_url
                    break
        idempotency_key = str(uuid4())

        # Execute send via Target API (live conversation loop OR async message)
        api_t0 = time.monotonic()
        api_step_id = uuid4().hex[:8]
        self.state.record_stage_start("target_api", mode=channel, step_id=api_step_id)
        recorded_events: list[Event] = []
        call_transcript: list[dict[str, Any]] = []
        try:
            if self.policy.is_live_channel(channel):
                recorded_events, call_transcript, won_in_call = await self._execute_live_conversation(
                    session_id=session_id,
                    channel=channel,
                    touch_spec=touch_spec,
                    initial_msg_prop=msg_prop,
                    initial_body=body,
                    initial_attachments=attachments,
                    initial_facts_used=facts_used,
                    sender_obj=sender_obj,
                    sent_at=sent_at,
                    registered_url=registered_url,
                    session_registered_urls=session_registered_urls,
                    actions=actions,
                )
                self.state.record_stage_timing(
                    "target_api",
                    elapsed_ms=int((time.monotonic() - api_t0) * 1000),
                    ok=True,
                    mode=channel,
                    step_id=api_step_id,
                )
                if won_in_call:
                    return self.snapshot
            else:
                try:
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
                except StoryTimeError:
                    sent_at = await self._recover_story_time(session_id)
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
                        idempotency_key=str(uuid4()),
                    )
                self.state.record_stage_timing(
                    "target_api",
                    elapsed_ms=int((time.monotonic() - api_t0) * 1000),
                    ok=True,
                    mode=channel,
                    step_id=api_step_id,
                )

                # Record outbound touch & returned target events
                self.state.record_outbound_turn(
                    channel=channel,
                    body=body,
                    subject=subject,
                    idempotency_key=idempotency_key,
                    facts_used=facts_used,
                    attachments=attachments,
                    cost_touch=True,
                    story_time=contact_sent_at,
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
                recorded_events = [
                    self.state.record_target_event(
                        ev, default_channel=channel, story_time=contact_sent_at
                    )
                    for ev in raw_events
                    if isinstance(ev, dict)
                ]
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
                campaign_memory=dict(self._campaign_memory),
                **self._strategy_context_kwargs(),
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

        had_reply = any(
            e.payload.get("event") in {"reply", "persona_says"} for e in recorded_events
        )
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

        # Track whether this touch carried a registered page URL
        for u in session_registered_urls:
            if u in body and u not in self._funnel_state["links_sent"]:
                self._funnel_state["links_sent"].append(u)

        # Classify non-credential page submissions & intermediate step events (link/attachment opens)
        page_submissions: list[str] = []
        for ev in recorded_events:
            ev_kind = str(ev.payload.get("event") or "").strip().lower()
            if ev_kind in {"link.opened", "attachment.opened"}:
                # Recorded as a positive step/momentum signal, NOT a terminal incident
                if ev_kind == "link.opened":
                    ev_data = ev.payload.get("data")
                    opened_url = str(
                        (ev_data.get("url") if isinstance(ev_data, dict) else "")
                        or ev.payload.get("url")
                        or registered_url
                        or ""
                    ).strip()
                    if opened_url and opened_url not in self._funnel_state["links_opened"]:
                        self._funnel_state["links_opened"].append(opened_url)
                boosted_susp = max(0.0, self.policy.state.last_suspicion - 0.10)
                self.policy.record_signal(
                    engagement=max(0.70, self.policy.state.last_engagement),
                    suspicion=boosted_susp,
                )
                continue
            sub_class = classify_page_submission(ev, registered_pages)
            if sub_class == "disclosure_candidate":
                summary = extract_page_submission_summary(ev)
                if summary:
                    page_submissions.append(summary)
            elif sub_class == "step_up":
                ev_data = ev.payload.get("data")
                sub_url = str(
                    (ev_data.get("url") if isinstance(ev_data, dict) else "")
                    or ev.payload.get("url")
                    or registered_url
                    or ""
                ).strip()
                if sub_url and sub_url not in self._funnel_state["step_up_submitted"]:
                    self._funnel_state["step_up_submitted"].append(sub_url)
                summary = extract_page_submission_summary(ev)
                if summary:
                    page_submissions.append(summary)
                boosted_susp = max(0.0, self.policy.state.last_suspicion - 0.15)
                self.policy.record_signal(engagement=0.75, suspicion=boosted_susp)

        # Signal extraction on replies (or final persona utterance for a live conversation)
        signal_events = [e for e in recorded_events if e.payload.get("event") == "reply"]
        if not signal_events:
            persona_evs = [
                e for e in recorded_events if e.payload.get("event") == "persona_says"
            ]
            if persona_evs:
                signal_events = [persona_evs[-1]]
        for ev in signal_events:
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

        # Profile update (batched, on reply/call transcript, or on page submission)
        disclosure_candidates: list[dict[str, Any]] | None = None
        action_candidates: list[dict[str, Any]] | None = None
        should_profile = (
            self.policy.should_trigger_profile()
            or had_reply
            or bool(page_submissions)
            or bool(call_transcript)
        )
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
                page_submissions=page_submissions or None,
                call_transcript=call_transcript or None,
            )
            if prof_prop is not None:
                actions.extend(self._actions_from_commits([self.state.commit_proposal(prof_prop)]))
                self.policy.mark_profile_ran()
                raw_disc = prof_prop.payload.get("disclosure_candidates") or prof_prop.payload.get(
                    "disclosures"
                )
                if isinstance(raw_disc, list):
                    disclosure_candidates = [d for d in raw_disc if isinstance(d, dict)]
                raw_act = prof_prop.payload.get("action_candidates") or prof_prop.payload.get(
                    "actions_taken"
                )
                if isinstance(raw_act, list):
                    action_candidates = [a for a in raw_act if isinstance(a, dict)]

        # Check disclosure / committed action win only when aligned with the active campaign objective
        active_plan = self.snapshot.campaign.plan
        allow_disclosure_win = active_plan is None or active_plan.objective == "disclosure"
        allow_action_win = active_plan is None or active_plan.objective == "action"
        disc_win = evaluate_wins(
            [],
            plan=active_plan,
            registered_pages=registered_pages,
            disclosure_candidates=disclosure_candidates if allow_disclosure_win else None,
            action_candidates=action_candidates if allow_action_win else None,
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
                campaign_memory=dict(self._campaign_memory),
                **self._strategy_context_kwargs(),
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
            else:
                await self._load_channels_catalog()
                await self._reconcile_resumed_session()
                if self._target_memory is None and self.snapshot.target.id:
                    self._target_memory = self.memory_store.load(
                        self.snapshot.run.scenario, self.snapshot.target.id
                    )

            cycles = 0
            while self.snapshot.run.state not in {"WIN", "ABORT", "DONE"} and cycles < max_cycles:
                await self.step()
                cycles += 1

            if self.snapshot.run.state in {"WIN", "DONE", "ABORT"}:
                exit_reason = "completed"
            elif cycles >= max_cycles:
                exit_reason = "cycle_limit"
        except asyncio.CancelledError:
            exit_reason = "stopped"
            raise
        except Exception:
            exit_reason = "service_failure"
            raise
        finally:
            await self._close_active_conversations()
            if (
                self.snapshot.run.state in {"WIN", "DONE"}
                and self.snapshot.sessions.current
            ):
                try:
                    await self.api.end_session(self.snapshot.sessions.current)
                except TargetAPIError:
                    pass
            if self.snapshot.run.state != "BOOT" and (
                self.snapshot.target.id or self.snapshot.run.target_id
            ):
                try:
                    self.memory_store.update_from_snapshot(self.snapshot)
                except Exception as exc:  # noqa: BLE001
                    self.state.record_system_error("memory", f"TargetMemory save skipped: {exc}")
            self.state.record_run_exit(exit_reason)
        return self.snapshot
