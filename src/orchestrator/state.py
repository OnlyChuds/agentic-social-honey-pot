from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, get_args

from pydantic import ValidationError

from src.models import (
    BrandingManifest,
    CampaignPlan,
    CampaignState,
    Channel,
    ConversationTurn,
    DecisionRecord,
    Event,
    Fact,
    Hook,
    Objective,
    Profile,
    Proposal,
    ProposalKind,
    QAVerdict,
    Snapshot,
    TargetInfo,
)
from src.store.db import Store
from src.store.memory import TargetMemory

VALID_CHANNELS: frozenset[str] = frozenset(get_args(Channel))

LEGAL_TRANSITIONS: dict[CampaignState, frozenset[CampaignState]] = {
    "BOOT": frozenset({"RESEARCH", "ABORT"}),
    "RESEARCH": frozenset({"ENGAGE", "WIN", "ABORT", "DONE"}),
    "ENGAGE": frozenset({"ENGAGE", "WIN", "ABORT", "DONE"}),
    "WIN": frozenset(),
    "ABORT": frozenset(),
    "DONE": frozenset({"ENGAGE"}),  # allowed only when starting a new session
}

PROPOSAL_SECTIONS: dict[ProposalKind, frozenset[str]] = {
    "facts": frozenset({"facts"}),
    "dossier": frozenset({"facts", "hooks", "profile"}),
    "profile_update": frozenset({"profile", "hooks"}),
    "hook_update": frozenset({"hooks"}),
    "identity": frozenset({"identity"}),
    "plan": frozenset({"campaign.plan"}),
    "message": frozenset(),
    "signals": frozenset(),
    "qa_verdict": frozenset(),
}


@dataclass(slots=True)
class CommitResult:
    accepted: bool
    proposal_id: str
    sections: list[str] = field(default_factory=list)
    reason: str = ""
    snapshot_version: int = 0


class StateTransitionError(RuntimeError):
    """Raised on an illegal campaign state transition."""


class StateManager:
    """Single-writer owner of Snapshot state, versioned proposal commits, and crash recovery."""

    def __init__(self, store: Store, snapshot: Snapshot | None = None) -> None:
        self.store = store
        self.snapshot = snapshot or Snapshot()
        if self.store.latest_snapshot() is None:
            self.store.save_snapshot(self.snapshot)

    @classmethod
    def recover(cls, store: Store) -> StateManager:
        """Recover the latest persisted snapshot and replay any unapplied tail events."""
        latest = store.latest_snapshot()
        if latest is None:
            return cls(store, Snapshot())

        manager = cls(store, latest.model_copy(deep=True))
        tail_events = store.get_events(since_version=manager.snapshot.version)
        recorded_event_refs = {t.event_ref for t in manager.snapshot.conversation.turns if t.event_ref}
        recorded_incident_events = {
            ev_id
            for inc in manager.snapshot.incidents
            for ev_id in inc.get("evidence", [])
        }

        replayed = False
        for ev in tail_events:
            if ev.type == "target_event" and ev.event_id not in recorded_event_refs:
                if ev.payload.get("event") == "reply":
                    raw_channel = str(ev.payload.get("channel") or "email")
                    ch: Channel = raw_channel if raw_channel in VALID_CHANNELS else "email"  # type: ignore[assignment]
                    turn_no = len(manager.snapshot.conversation.turns) + 1
                    manager.snapshot.conversation.turns.append(
                        ConversationTurn(
                            n=turn_no,
                            ts=ev.ts,
                            dir="in",
                            channel=ch,
                            body=str(ev.payload.get("body") or ""),
                            event_ref=ev.event_id,
                        )
                    )
                    recorded_event_refs.add(ev.event_id)
                    replayed = True
            elif ev.type == "incident":
                ev_ids = ev.payload.get("evidence", [])
                if not any(eid in recorded_incident_events for eid in ev_ids):
                    manager.snapshot.incidents.append(dict(ev.payload))
                    manager.snapshot.run.state = "WIN"
                    replayed = True

        if replayed:
            manager.snapshot.version += 1
            store.save_snapshot(manager.snapshot)
        return manager

    def _bump_and_save(self) -> int:
        self.snapshot.version += 1
        self.store.save_snapshot(self.snapshot)
        return self.snapshot.version

    # --- Setup & Session Lifecycle ---

    def seed_from_memory(self, memory: TargetMemory) -> list[str]:
        """Seed initial facts, hooks, and profile hints from cross-session TargetMemory."""
        if memory.sessions_seen <= 0:
            return []
        seeded_sections: list[str] = []

        if memory.verified_facts and not self.snapshot.facts:
            self._merge_facts([f.model_copy(deep=True) for f in memory.verified_facts])
            seeded_sections.append("facts")

        if memory.hook_scoreboard and not self.snapshot.hooks:
            seeded_hooks = [
                Hook(
                    hook_id=rec.hook_id,
                    topic=rec.topic,
                    evidence=list(rec.evidence),
                    why_leverage=rec.why_leverage,
                    confidence=rec.confidence,
                    status=rec.status if rec.status in {"candidate", "active", "exhausted"} else "candidate",  # type: ignore[arg-type]
                )
                for rec in memory.hook_scoreboard.values()
                if rec.topic
            ]
            if seeded_hooks:
                self._merge_hooks(seeded_hooks)
                seeded_sections.append("hooks")

        prof_changed = False
        if memory.watchouts:
            seen = {w.strip().lower() for w in self.snapshot.profile.watchouts}
            for w in memory.watchouts:
                if w.strip() and w.strip().lower() not in seen:
                    self.snapshot.profile.watchouts.append(w.strip())
                    seen.add(w.strip().lower())
                    prof_changed = True
        if memory.style_guide and not self.snapshot.profile.style_guide:
            self.snapshot.profile.style_guide = memory.style_guide
            prof_changed = True
        if memory.trust_style and not self.snapshot.profile.trust_style:
            self.snapshot.profile.trust_style = memory.trust_style
            prof_changed = True
        if prof_changed:
            seeded_sections.append("profile")

        if seeded_sections:
            self._bump_and_save()
        return seeded_sections

    def reconcile_remote_session(
        self,
        remote_session: dict[str, Any],
        remote_events: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Reconcile local snapshot counters against remote Target API session state on resume."""
        changed = False
        remote_touches = remote_session.get("touches_used")
        if isinstance(remote_touches, int) and remote_touches > self.snapshot.campaign.budget.used:
            self.snapshot.campaign.budget.used = remote_touches
            changed = True

        if remote_session.get("ended") is True and self.snapshot.run.state == "ENGAGE":
            self.snapshot.run.state = "DONE"
            changed = True

        if remote_events:
            existing_bodies = {
                (t.dir, t.channel, t.body) for t in self.snapshot.conversation.turns
            }
            for raw_ev in remote_events:
                if not isinstance(raw_ev, dict):
                    continue
                ev_type = str(raw_ev.get("type") or raw_ev.get("event") or "")
                body = str(raw_ev.get("body") or raw_ev.get("text") or raw_ev.get("say") or "")
                ch_raw = str(raw_ev.get("channel") or "email")
                ch: Channel = ch_raw if ch_raw in VALID_CHANNELS else "email"  # type: ignore[assignment]
                if ev_type in {"reply", "persona_says"} and ("in", ch, body) not in existing_bodies:
                    self.record_target_event(raw_ev, default_channel=ch)
                    existing_bodies.add(("in", ch, body))
                    changed = True

        if changed:
            self._bump_and_save()
        return changed

    def configure_run(
        self,
        *,
        scenario: str,
        target: TargetInfo,
        run_no: int,
        session_max_touches: int,
        sessions_allowed: int,
    ) -> int:
        self.snapshot.run.scenario = "eval" if scenario == "eval" else "practice"
        self.snapshot.run.target_id = target.id
        self.snapshot.run.run_no = run_no
        self.snapshot.target = target
        self.snapshot.campaign.budget.session_max = session_max_touches
        self.snapshot.sessions.allowed = sessions_allowed
        return self._bump_and_save()

    def start_session(self, session_id: str, *, session_max_touches: int | None = None) -> Event:
        """Start or restart a Target API session while retaining shared memory across sessions."""
        self.snapshot.sessions.used += 1
        self.snapshot.sessions.current = session_id
        self.snapshot.conversation.session_id = session_id
        self.snapshot.conversation.turns = []
        self.snapshot.campaign.budget.used = 0
        if session_max_touches is not None:
            self.snapshot.campaign.budget.session_max = session_max_touches

        ev = Event(
            type="session",
            actor="orchestrator",
            base_version=self.snapshot.version,
            payload={
                "action": "started",
                "session_id": session_id,
                "sessions_used": self.snapshot.sessions.used,
                "session_max_touches": self.snapshot.campaign.budget.session_max,
            },
        )
        self.store.append_event(ev)
        self._bump_and_save()
        return ev

    def transition_state(self, new_state: CampaignState, *, reason: str = "") -> Event:
        current = self.snapshot.run.state
        if new_state != current and new_state not in LEGAL_TRANSITIONS.get(current, frozenset()):
            raise StateTransitionError(f"Illegal state transition: {current} -> {new_state}")
        self.snapshot.run.state = new_state
        ev = Event(
            type="system",
            actor="orchestrator",
            base_version=self.snapshot.version,
            payload={
                "kind": "state_transition",
                "from": current,
                "to": new_state,
                "reason": reason,
            },
        )
        self.store.append_event(ev)
        self._bump_and_save()
        return ev

    # --- Proposal Validation & Commits ---

    def validate_message_proposal(
        self,
        proposal: Proposal,
        *,
        registered_page_urls: set[str] | None = None,
    ) -> tuple[bool, str]:
        if proposal.base_version != self.snapshot.version:
            return (
                False,
                f"stale base_version ({proposal.base_version} != {self.snapshot.version})",
            )

        payload = proposal.payload
        channel = str(payload.get("channel") or "")
        if channel not in VALID_CHANNELS:
            return False, f"invalid channel: {channel!r}"

        body = str(payload.get("body") or "").strip()
        if not body:
            return False, "message body is empty"

        if self.snapshot.campaign.budget.used >= self.snapshot.campaign.budget.session_max:
            return False, "touch budget exhausted"

        facts_by_id = {f.fact_id: f for f in self.snapshot.facts}
        raw_facts_used = payload.get("facts_used", [])
        if not isinstance(raw_facts_used, list):
            return False, "facts_used must be a list of fact IDs"

        is_first_contact = self.snapshot.campaign.budget.used == 0
        for fid in raw_facts_used:
            fid_str = str(fid)
            fact = facts_by_id.get(fid_str)
            if fact is None:
                return False, f"unknown fact_id in facts_used: {fid_str}"
            if fact.status != "active":
                return False, f"superseded fact_id in facts_used: {fid_str}"
            if is_first_contact and fact.provenance == "inferred" and fact.confidence < 0.5:
                msg = (
                    f"low-confidence inferred fact {fid_str} ({fact.confidence}) "
                    "disallowed on first contact"
                )
                return False, msg

        page_request = payload.get("page_request")
        if page_request:
            req_str = str(page_request)
            known_urls = registered_page_urls or set()
            if not any(url == req_str or url.endswith(req_str) for url in known_urls):
                return (
                    False,
                    f"page_request {req_str!r} is not registered in the current session",
                )

        return True, ""

    def _validate_and_prepare(self, proposal: Proposal) -> tuple[bool, str, dict[str, Any]]:
        """Validate proposal schema against Pydantic models without mutating state."""
        kind = proposal.kind
        payload = proposal.payload
        prepared: dict[str, Any] = {}

        try:
            if kind == "facts":
                raw_facts = payload.get("facts", [])
                prepared["facts"] = [Fact.model_validate(f) for f in raw_facts]
            elif kind == "dossier":
                raw_facts = payload.get("facts", [])
                raw_hooks = payload.get("hook_seeds", payload.get("hooks", []))
                raw_psych = payload.get("psychoprofile", [])
                prepared["facts"] = [Fact.model_validate(f) for f in raw_facts]
                prepared["hooks"] = [Hook.model_validate(h) for h in raw_hooks]
                prepared["psychoprofile"] = raw_psych
            elif kind == "profile_update":
                prof_raw = payload.get("profile_update", payload.get("profile", payload))
                if not isinstance(prof_raw, dict):
                    return False, "profile_update payload must be an object", {}
                prepared["profile"] = self._merge_profile_payload(prof_raw)
                raw_hooks = list(payload.get("hook_updates", []) or [])
                raw_new_hooks = payload.get("new_hooks", [])
                if isinstance(raw_new_hooks, list):
                    raw_hooks.extend(raw_new_hooks)
                prepared["hooks"] = self._validate_hook_updates(raw_hooks)
            elif kind == "hook_update":
                raw_hooks = payload.get("hook_updates", payload.get("hooks", []))
                prepared["hooks"] = self._validate_hook_updates(raw_hooks)
            elif kind == "identity":
                manifest_raw = payload.get("manifest", payload)
                prepared["identity"] = BrandingManifest.model_validate(manifest_raw)
            elif kind == "plan":
                plan_raw = payload.get("plan", payload)
                plan = CampaignPlan.model_validate(plan_raw)
                if len(plan.touches) > self.snapshot.campaign.budget.session_max:
                    msg = (
                        f"plan has {len(plan.touches)} touches exceeding "
                        f"session_max={self.snapshot.campaign.budget.session_max}"
                    )
                    return False, msg, {}
                prepared["plan"] = plan
            elif kind == "qa_verdict":
                prepared["qa_verdict"] = QAVerdict.model_validate(payload)
            elif kind == "signals":
                float(payload.get("engagement", 0.0))
                float(payload.get("suspicion", 0.0))
                prepared["signals"] = dict(payload)
            elif kind == "message":
                ok, reason = self.validate_message_proposal(proposal)
                if not ok:
                    return False, reason, {}
                prepared["message"] = dict(payload)
        except (ValidationError, TypeError, ValueError) as exc:
            return False, f"schema validation failed: {exc}", {}

        return True, "", prepared

    def _merge_profile_payload(self, raw_update: dict[str, Any]) -> Profile:
        """Merge a full or partial profile_update dict into the existing Profile without wiping fields."""
        base = self.snapshot.profile.model_dump(mode="json")

        for scalar in ("style_guide", "trust_style", "timezone_hint", "language"):
            val = raw_update.get(scalar)
            if isinstance(val, str) and val.strip():
                base[scalar] = val

        if "overall_confidence" in raw_update and raw_update["overall_confidence"] is not None:
            base["overall_confidence"] = raw_update["overall_confidence"]

        if isinstance(raw_update.get("watchouts"), list):
            seen: set[str] = set()
            merged_watchouts: list[str] = []
            for item in [*base.get("watchouts", []), *raw_update["watchouts"]]:
                text = str(item).strip()
                if text and text not in seen:
                    seen.add(text)
                    merged_watchouts.append(text)
            base["watchouts"] = merged_watchouts

        if isinstance(raw_update.get("psych"), list) and raw_update["psych"]:
            by_trait: dict[str, dict[str, Any]] = {
                str(t.get("trait", "")): dict(t) for t in base.get("psych", []) if t.get("trait")
            }
            for item in raw_update["psych"]:
                if isinstance(item, dict) and item.get("trait"):
                    by_trait[str(item["trait"])] = dict(item)
            base["psych"] = list(by_trait.values())

        if isinstance(raw_update.get("stressors"), list) and raw_update["stressors"]:
            by_topic: dict[str, dict[str, Any]] = {
                str(s.get("topic", "")): dict(s) for s in base.get("stressors", []) if s.get("topic")
            }
            for item in raw_update["stressors"]:
                if isinstance(item, dict) and item.get("topic"):
                    by_topic[str(item["topic"])] = dict(item)
            base["stressors"] = list(by_topic.values())

        return Profile.model_validate(base)

    def _validate_hook_updates(self, raw_hooks: Any) -> list[Hook]:
        """Validate full or partial hook updates, filling unchanged fields from existing hooks."""
        if not isinstance(raw_hooks, list):
            raise TypeError("hook_updates must be a list")
        existing_by_id = {h.hook_id: h.model_dump(mode="json") for h in self.snapshot.hooks}
        validated: list[Hook] = []
        for item in raw_hooks:
            if not isinstance(item, dict):
                raise TypeError("each hook update must be an object")
            hid = str(item.get("hook_id") or "")
            if hid and hid in existing_by_id:
                merged = dict(existing_by_id[hid])
                for k, v in item.items():
                    if v is not None:
                        merged[k] = v
                validated.append(Hook.model_validate(merged))
            elif str(item.get("topic") or "").strip():
                validated.append(Hook.model_validate(item))
        return validated

    def _merge_facts(self, incoming: list[Fact]) -> None:
        by_id = {f.fact_id: f for f in self.snapshot.facts}
        for fact in incoming:
            if fact.fact_id in by_id:
                existing = by_id[fact.fact_id]
                if existing.claim != fact.claim:
                    existing.status = "superseded"
                    replacement = fact.model_copy(deep=True)
                    base_id = fact.fact_id.split("_v", 1)[0]
                    rev = 2
                    while f"{base_id}_v{rev}" in by_id:
                        rev += 1
                    replacement.fact_id = f"{base_id}_v{rev}"
                    existing.superseded_by = replacement.fact_id
                    self.snapshot.facts.append(replacement)
                    by_id[replacement.fact_id] = replacement
                else:
                    existing.confidence = max(existing.confidence, fact.confidence)
                    if fact.source and not existing.source:
                        existing.source = fact.source
                    if fact.osint_grade and not existing.osint_grade:
                        existing.osint_grade = fact.osint_grade
            else:
                self.snapshot.facts.append(fact)
                by_id[fact.fact_id] = fact

    def _merge_hooks(self, incoming: list[Hook]) -> None:
        by_id = {h.hook_id: h for h in self.snapshot.hooks}
        for hook in incoming:
            if hook.hook_id in by_id:
                idx = next(
                    i for i, existing in enumerate(self.snapshot.hooks)
                    if existing.hook_id == hook.hook_id
                )
                self.snapshot.hooks[idx] = hook
            else:
                self.snapshot.hooks.append(hook)
                by_id[hook.hook_id] = hook

    def _apply_prepared(self, kind: ProposalKind, prepared: dict[str, Any]) -> list[str]:
        sections: list[str] = []
        if "facts" in prepared:
            self._merge_facts(prepared["facts"])
            sections.append("facts")
        if "hooks" in prepared:
            self._merge_hooks(prepared["hooks"])
            sections.append("hooks")
        if "profile" in prepared:
            self.snapshot.profile = prepared["profile"]
            sections.append("profile")
        elif prepared.get("psychoprofile"):
            existing_profile = self.snapshot.profile.model_dump()
            existing_profile["psych"] = prepared["psychoprofile"]
            try:
                self.snapshot.profile = Profile.model_validate(existing_profile)
                sections.append("profile")
            except ValidationError:
                pass
        if "identity" in prepared:
            self.snapshot.identity = prepared["identity"]
            sections.append("identity")
        if "plan" in prepared:
            self.snapshot.campaign.plan = prepared["plan"]
            sections.append("campaign.plan")
        return sections

    def commit_proposal(self, proposal: Proposal) -> CommitResult:
        """Validate and commit a single proposal, rejecting stale base_version."""
        if proposal.base_version != self.snapshot.version:
            return CommitResult(
                accepted=False,
                proposal_id=proposal.proposal_id,
                reason=f"stale base_version ({proposal.base_version} != {self.snapshot.version})",
                snapshot_version=self.snapshot.version,
            )

        ok, reason, prepared = self._validate_and_prepare(proposal)
        if not ok:
            return CommitResult(
                accepted=False,
                proposal_id=proposal.proposal_id,
                reason=reason,
                snapshot_version=self.snapshot.version,
            )

        sections = self._apply_prepared(proposal.kind, prepared)
        self.store.append_event(
            Event(
                type="agent_output",
                actor=proposal.agent,
                base_version=proposal.base_version,
                payload={
                    "proposal_id": proposal.proposal_id,
                    "kind": proposal.kind,
                    "sections": sections,
                    "rationale": proposal.rationale,
                    "payload": proposal.payload,
                },
            )
        )
        new_version = self._bump_and_save() if sections else self.snapshot.version
        return CommitResult(
            accepted=True,
            proposal_id=proposal.proposal_id,
            sections=sections,
            snapshot_version=new_version,
        )

    def commit_batch(self, proposals: list[Proposal]) -> list[CommitResult]:
        """Atomically commit concurrent disjoint-section proposals from the same base_version."""
        current_version = self.snapshot.version
        claimed_sections: set[str] = set()
        results: list[CommitResult] = []
        any_mutated = False

        for proposal in proposals:
            if proposal.base_version != current_version:
                results.append(
                    CommitResult(
                        accepted=False,
                        proposal_id=proposal.proposal_id,
                        reason=f"stale base_version ({proposal.base_version} != {current_version})",
                        snapshot_version=current_version,
                    )
                )
                continue

            target_sections = PROPOSAL_SECTIONS.get(proposal.kind, frozenset())
            overlap = claimed_sections & target_sections
            if overlap:
                results.append(
                    CommitResult(
                        accepted=False,
                        proposal_id=proposal.proposal_id,
                        reason=f"conflicting batch write to sections {sorted(overlap)}",
                        snapshot_version=current_version,
                    )
                )
                continue

            ok, reason, prepared = self._validate_and_prepare(proposal)
            if not ok:
                results.append(
                    CommitResult(
                        accepted=False,
                        proposal_id=proposal.proposal_id,
                        reason=reason,
                        snapshot_version=current_version,
                    )
                )
                continue

            sections = self._apply_prepared(proposal.kind, prepared)
            claimed_sections.update(sections)
            if sections:
                any_mutated = True
            self.store.append_event(
                Event(
                    type="agent_output",
                    actor=proposal.agent,
                    base_version=proposal.base_version,
                    payload={
                        "proposal_id": proposal.proposal_id,
                        "kind": proposal.kind,
                        "sections": sections,
                        "rationale": proposal.rationale,
                        "payload": proposal.payload,
                    },
                )
            )
            results.append(
                CommitResult(
                    accepted=True,
                    proposal_id=proposal.proposal_id,
                    sections=sections,
                    snapshot_version=current_version + 1 if sections else current_version,
                )
            )

        if any_mutated:
            new_v = self._bump_and_save()
            for r in results:
                if r.accepted:
                    r.snapshot_version = new_v
        return results

    # --- Conversation, Target Events, Incidents, Decisions ---

    def record_outbound_turn(
        self,
        *,
        channel: Channel,
        body: str,
        subject: str | None = None,
        idempotency_key: str = "",
        facts_used: list[str] | None = None,
        attachments: list[str | dict[str, Any]] | None = None,
        cost_touch: bool = True,
    ) -> Event:
        turn_no = len(self.snapshot.conversation.turns) + 1
        ev = Event(
            type="agent_output",
            actor="messaging",
            base_version=self.snapshot.version,
            payload={
                "direction": "out",
                "channel": channel,
                "subject": subject,
                "body": body,
                "idempotency_key": idempotency_key,
                "facts_used": facts_used or [],
                "attachments": list(attachments) if attachments else [],
            },
        )
        self.store.append_event(ev)
        self.snapshot.conversation.turns.append(
            ConversationTurn(
                n=turn_no,
                ts=ev.ts,
                dir="out",
                channel=channel,
                body=body,
                event_ref=ev.event_id,
            )
        )
        if cost_touch:
            self.snapshot.campaign.budget.used += 1
        self._bump_and_save()
        return ev

    def record_target_event(self, raw_event: dict[str, Any], *, default_channel: Channel) -> Event:
        event_name = str(raw_event.get("type") or raw_event.get("event") or "unknown")
        raw_channel = str(raw_event.get("channel") or default_channel)
        channel: Channel = raw_channel if raw_channel in VALID_CHANNELS else default_channel  # type: ignore[assignment]
        body = str(raw_event.get("body") or raw_event.get("text") or raw_event.get("say") or "")
        data = {
            k: v
            for k, v in raw_event.items()
            if k not in {"type", "event", "channel", "body", "text"}
        }

        ev = Event(
            type="target_event",
            actor="target",
            base_version=self.snapshot.version,
            payload={
                "event": event_name,
                "channel": channel,
                "body": body,
                "data": data,
            },
        )
        self.store.append_event(ev)

        if event_name in {"reply", "persona_says"}:
            turn_no = len(self.snapshot.conversation.turns) + 1
            self.snapshot.conversation.turns.append(
                ConversationTurn(
                    n=turn_no,
                    ts=ev.ts,
                    dir="in",
                    channel=channel,
                    body=body,
                    event_ref=ev.event_id,
                )
            )
        self._bump_and_save()
        return ev

    def record_incident(
        self,
        *,
        objective: Objective,
        evidence_event_ids: list[str],
        summary: str,
        details: dict[str, Any] | None = None,
    ) -> Event:
        incident_payload: dict[str, Any] = {
            "objective": objective,
            "evidence": list(evidence_event_ids),
            "summary": summary,
            "details": details or {},
            "session_id": self.snapshot.sessions.current,
            "touches_used": self.snapshot.campaign.budget.used,
        }
        ev = Event(
            type="incident",
            actor="orchestrator",
            base_version=self.snapshot.version,
            payload=incident_payload,
        )
        self.store.append_event(ev)
        self.snapshot.incidents.append(incident_payload)
        self.snapshot.run.state = "WIN"
        self._bump_and_save()
        return ev

    def record_system_error(self, actor: str, error: str, *, context: dict[str, Any] | None = None) -> Event:
        ev = Event(
            type="system",
            actor="orchestrator",
            base_version=self.snapshot.version,
            payload={
                "kind": "agent_error",
                "failed_actor": actor,
                "error": error,
                "context": context or {},
            },
        )
        self.store.append_event(ev)
        return ev

    def record_stage_start(
        self,
        stage: str,
        *,
        mode: str | None = None,
        step_id: str | None = None,
    ) -> Event:
        ev = Event(
            type="system",
            actor="orchestrator",
            base_version=self.snapshot.version,
            payload={
                "kind": "stage_start",
                "stage": stage,
                "mode": mode or "",
                "step_id": step_id or "",
            },
        )
        self.store.append_event(ev)
        return ev

    def record_stage_timing(
        self,
        stage: str,
        *,
        elapsed_ms: int,
        ok: bool,
        mode: str | None = None,
        step_id: str | None = None,
    ) -> Event:
        ev = Event(
            type="system",
            actor="orchestrator",
            base_version=self.snapshot.version,
            payload={
                "kind": "stage_timing",
                "stage": stage,
                "mode": mode or "",
                "step_id": step_id or "",
                "elapsed_ms": max(0, int(elapsed_ms)),
                "ok": ok,
            },
        )
        self.store.append_event(ev)
        return ev

    def record_run_exit(self, reason: str) -> Event:
        ev = Event(
            type="system",
            actor="orchestrator",
            base_version=self.snapshot.version,
            payload={
                "kind": "run_exit",
                "reason": reason,
            },
        )
        self.store.append_event(ev)
        return ev

    def log_decision(self, record: DecisionRecord) -> int:
        return self.store.append_decision(record)
