from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from src.agents.base import BaseAgent
from src.models import (
    Actor,
    Hook,
    HookEngagement,
    HookStatus,
    Profile,
    Proposal,
    ProposalKind,
    PsychTrait,
    Snapshot,
)


class HookUpdate(BaseModel):
    hook_id: str
    engagement: HookEngagement = Field(default_factory=HookEngagement)
    status: HookStatus = "active"
    rationale: str = ""


class DisclosureCandidate(BaseModel):
    claim: str
    evidence_event: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    public_or_not: Literal["non_public", "public", "uncertain"] = "uncertain"


class ProfileProposalPayload(BaseModel):
    profile_update: Profile = Field(default_factory=Profile)
    hook_updates: list[HookUpdate] = Field(default_factory=list)
    new_hooks: list[Hook] = Field(default_factory=list)
    disclosures: list[DisclosureCandidate] = Field(default_factory=list)
    rationale: str = ""


def _enforce_profile_invariants(
    payload: ProfileProposalPayload,
    snapshot: Snapshot,
) -> ProfileProposalPayload:
    active_fact_ids = {f.fact_id for f in snapshot.facts if f.status == "active"}
    event_refs = {t.event_ref for t in snapshot.conversation.turns if t.event_ref}
    valid_anchors = active_fact_ids | event_refs

    # Enforce 2-deflection exhaustion rule and clamp engagement scores
    for upd in payload.hook_updates:
        upd.engagement.score = max(0.0, min(1.0, upd.engagement.score))
        if upd.engagement.touches_touched >= 2 and upd.engagement.positive == 0:
            upd.status = "exhausted"

    # New candidate hooks must be grounded in at least one valid fact_id
    if active_fact_ids:
        grounded_hooks: list[Hook] = []
        for hook in payload.new_hooks:
            hook.evidence = [fid for fid in hook.evidence if fid in active_fact_ids]
            if hook.evidence:
                hook.status = "candidate"
                grounded_hooks.append(hook)
        payload.new_hooks = grounded_hooks

    # Keep only evidence-anchored psych traits when anchors exist
    if valid_anchors:
        payload.profile_update.psych = [
            t for t in payload.profile_update.psych if any(e in valid_anchors for e in t.evidence)
        ]

    return payload


class ProfileAgent(BaseAgent):
    """Maintains target Profile, Hook engagement scores, and non-public Disclosure classification."""

    agent_name: Actor = "profile"
    proposal_kind: ProposalKind = "profile_update"

    async def run(
        self,
        snapshot: Snapshot,
        *,
        mode: str | None = None,
        psychoprofile_seeds: list[PsychTrait] | None = None,
        recent_signals: list[dict[str, Any]] | None = None,
        **_extra: Any,
    ) -> Proposal:
        del mode
        active_facts = [f.model_dump(mode="json") for f in snapshot.facts if f.status == "active"]
        context: dict[str, Any] = {
            "target": snapshot.target.model_dump(mode="json"),
            "scenario": snapshot.run.scenario,
            "active_facts": active_facts,
            "current_hooks": [h.model_dump(mode="json") for h in snapshot.hooks],
            "current_profile": snapshot.profile.model_dump(mode="json"),
            "psychoprofile_seeds": [
                s.model_dump(mode="json") for s in (psychoprofile_seeds or [])
            ],
            "recent_conversation": [t.model_dump(mode="json") for t in snapshot.conversation.turns],
            "messaging_signals": recent_signals or [],
            "output_keys": [
                "profile_update ({style_guide, psych, stressors, trust_style, watchouts, timezone_hint, language, overall_confidence})",
                "hook_updates (list of {hook_id, engagement: {touches_touched, positive, score}, status, rationale})",
                "new_hooks (list of {hook_id, topic, evidence, why_leverage, confidence, status})",
                "disclosures (list of {claim, evidence_event, confidence, public_or_not})",
                "rationale (str)",
            ],
        }

        payload = await self.run_json(context, ProfileProposalPayload)
        payload = _enforce_profile_invariants(payload, snapshot)

        rationale = payload.rationale or (
            f"Updated profile (confidence={payload.profile_update.overall_confidence:.2f}), "
            f"{len(payload.hook_updates)} hook updates, {len(payload.new_hooks)} new hooks, "
            f"{len(payload.disclosures)} disclosure candidates."
        )
        return self.make_proposal(snapshot, payload, rationale=rationale)
