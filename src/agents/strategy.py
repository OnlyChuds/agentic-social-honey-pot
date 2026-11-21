from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.agents.base import BaseAgent
from src.config import AppConfig
from src.llm.text import TextLLMClient
from src.models import Actor, CampaignPlan, Proposal, ProposalKind, Snapshot


def _highest_seen_escalation(snapshot: Snapshot) -> int:
    """Return the highest planned escalation already executed in the current session."""
    if snapshot.campaign.plan is None:
        return 0
    used = snapshot.campaign.budget.used
    seen = [t.escalation for t in snapshot.campaign.plan.touches if t.n <= used]
    return max(seen, default=0)


def validate_campaign_plan(
    plan: CampaignPlan,
    snapshot: Snapshot,
    *,
    max_touches: int,
    pivot_reason: str | None = None,
) -> list[str]:
    """Validate deterministic Strategy invariants from prompts/strategy.md."""
    errors: list[str] = []
    used = snapshot.campaign.budget.used
    session_max = snapshot.campaign.budget.session_max
    exhausted_hooks = {h.hook_id for h in snapshot.hooks if h.status == "exhausted"}

    if not plan.touches:
        errors.append("plan.touches must contain at least 1 touch.")
    if len(plan.touches) > max_touches:
        errors.append(
            f"plan.touches has {len(plan.touches)} entries, exceeding remaining budget {max_touches}."
        )
    if plan.primary_hook and plan.primary_hook in exhausted_hooks:
        errors.append(f"primary_hook '{plan.primary_hook}' is exhausted.")

    expected_start = used + 1
    seen_nums: set[int] = set()
    min_escalation = _highest_seen_escalation(snapshot) if pivot_reason else 1

    for idx, touch in enumerate(plan.touches):
        if touch.n in seen_nums:
            errors.append(f"Duplicate touch number n={touch.n}.")
        seen_nums.add(touch.n)
        if touch.n > session_max:
            errors.append(f"Touch n={touch.n} exceeds session_max={session_max}.")
        if touch.n != expected_start + idx:
            errors.append(
                f"Touch numbers must be contiguous starting at {expected_start} "
                f"(got n={touch.n} at index {idx})."
            )
        if touch.hook and touch.hook in exhausted_hooks:
            errors.append(f"Touch n={touch.n} uses exhausted hook '{touch.hook}'.")
        if not 1 <= touch.escalation <= 5:
            errors.append(f"Touch n={touch.n} escalation={touch.escalation} must be in 1..5.")
        if pivot_reason and touch.escalation < min_escalation:
            errors.append(
                f"Pivot touch n={touch.n} escalation={touch.escalation} drops below "
                f"already-seen escalation {min_escalation}."
            )

    if plan.objective == "credentials":
        if plan.landing_page is None:
            errors.append("objective='credentials' requires a non-null landing_page spec.")
        else:
            planned_nums = {t.n for t in plan.touches}
            if plan.landing_page.trigger_touch not in planned_nums:
                errors.append(
                    f"landing_page.trigger_touch={plan.landing_page.trigger_touch} "
                    f"must match one of the planned touches {sorted(planned_nums)}."
                )
            elif len(plan.touches) >= 2 and plan.landing_page.trigger_touch == expected_start:
                errors.append(
                    "landing_page.trigger_touch cannot be the very first planned touch "
                    "(page must be registered in the cycle before linking)."
                )

    return errors


def _clamp_plan_to_budget(
    plan: CampaignPlan,
    snapshot: Snapshot,
    max_touches: int,
) -> CampaignPlan:
    """Last-resort deterministic guard so a plan can never violate hard budget or exhausted hooks."""
    used = snapshot.campaign.budget.used
    exhausted = {h.hook_id for h in snapshot.hooks if h.status == "exhausted"}
    fallback_hook = next(
        (h.hook_id for h in snapshot.hooks if h.status != "exhausted"),
        plan.primary_hook if plan.primary_hook not in exhausted else "",
    )
    if plan.primary_hook in exhausted:
        plan.primary_hook = fallback_hook

    trimmed = plan.touches[:max_touches]
    for idx, touch in enumerate(trimmed):
        touch.n = used + 1 + idx
        touch.escalation = max(1, min(5, touch.escalation))
        if touch.hook in exhausted:
            touch.hook = fallback_hook
    plan.touches = trimmed

    if plan.landing_page is not None and trimmed:
        valid_nums = [t.n for t in trimmed]
        if plan.landing_page.trigger_touch not in valid_nums:
            plan.landing_page.trigger_touch = valid_nums[-1]
    return plan


class StrategyAgent(BaseAgent):
    """Campaign planner converting Profile, ranked Hooks, and remaining budget into a CampaignPlan."""

    agent_name: Actor = "strategy"
    proposal_kind: ProposalKind = "plan"

    def __init__(
        self,
        llm: TextLLMClient,
        *,
        config: AppConfig | None = None,
        prompts_dir: Path = Path("prompts"),
    ) -> None:
        super().__init__(llm, prompts_dir=prompts_dir)
        self.config = config

    async def run(
        self,
        snapshot: Snapshot,
        *,
        mode: str | None = None,
        reason: str | None = None,
        pivot_reason: str | None = None,
        aggressive: bool = False,
        max_repairs: int = 1,
        **_extra: Any,
    ) -> Proposal:
        del mode
        if pivot_reason is None and reason is not None:
            pivot_reason = reason
        used = snapshot.campaign.budget.used
        remaining = max(0, snapshot.campaign.budget.session_max - used)
        if aggressive:
            cap = (
                self.config.aggressive.budget.compressed_plan_touches
                if self.config is not None
                else 8
            )
            max_touches = min(remaining, cap)
        else:
            max_touches = remaining

        ranked_hooks = sorted(
            [h for h in snapshot.hooks if h.status != "exhausted"],
            key=lambda h: (h.engagement.score, h.confidence),
            reverse=True,
        )
        exhausted_hooks = [h.hook_id for h in snapshot.hooks if h.status == "exhausted"]
        tried_channels = [
            {"n": t.n, "channel": t.channel, "dir": t.dir} for t in snapshot.conversation.turns
        ]

        context: dict[str, Any] = {
            "run": snapshot.run.model_dump(mode="json"),
            "target": snapshot.target.model_dump(mode="json"),
            "budget": {
                "session_max": snapshot.campaign.budget.session_max,
                "used": used,
                "remaining_to_plan": max_touches,
                "first_planned_touch_n": used + 1,
            },
            "sessions": snapshot.sessions.model_dump(mode="json"),
            "pivot_reason": pivot_reason,
            "highest_seen_escalation": _highest_seen_escalation(snapshot),
            "ranked_active_hooks": [h.model_dump(mode="json") for h in ranked_hooks],
            "exhausted_hooks_DO_NOT_USE": exhausted_hooks,
            "profile": snapshot.profile.model_dump(mode="json"),
            "active_facts": [
                f.model_dump(mode="json") for f in snapshot.facts if f.status == "active"
            ],
            "identity": snapshot.identity.model_dump(mode="json") if snapshot.identity else None,
            "previous_plan": (
                snapshot.campaign.plan.model_dump(mode="json") if snapshot.campaign.plan else None
            ),
            "conversation_history": tried_channels,
            "output_keys": [
                "objective ('credentials' | 'action' | 'disclosure')",
                "primary_hook (hook_id str)",
                "touches (list of {n, channel, intent, hook, escalation, stop_if})",
                "landing_page ({pretext, fields, trigger_touch, url_path} or null)",
                "action_artifact ({filename, purpose} or null)",
                "pivot_triggers (list[str])",
                "give_up (str)",
                "session_restart ({recommended: bool, reason: str | null})",
            ],
        }

        messages = self.build_messages(context)
        plan = await self.llm.complete_json(
            messages,
            CampaignPlan,
            temperature=self.prompt.temperature,
            max_tokens=self.prompt.max_tokens,
        )

        violations = validate_campaign_plan(
            plan, snapshot, max_touches=max_touches, pivot_reason=pivot_reason
        )
        for _ in range(max_repairs):
            if not violations:
                break
            repair_messages = [
                *messages,
                {"role": "assistant", "content": plan.model_dump_json()},
                {
                    "role": "user",
                    "content": (
                        "Your plan violated hard campaign constraints:\n"
                        + "\n".join(f"- {v}" for v in violations)
                        + "\nReturn a corrected JSON CampaignPlan fixing every violation."
                    ),
                },
            ]
            plan = await self.llm.complete_json(
                repair_messages,
                CampaignPlan,
                temperature=0.2,
                max_tokens=self.prompt.max_tokens,
            )
            violations = validate_campaign_plan(
                plan, snapshot, max_touches=max_touches, pivot_reason=pivot_reason
            )

        if violations:
            plan = _clamp_plan_to_budget(plan, snapshot, max_touches)

        rationale = (
            f"Planned {len(plan.touches)} touches (objective={plan.objective}, "
            f"primary_hook={plan.primary_hook})"
            + (f" [pivot: {pivot_reason}]" if pivot_reason else "")
        )
        return self.make_proposal(
            snapshot,
            json.loads(plan.model_dump_json()),
            rationale=rationale,
        )
