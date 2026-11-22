from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from src.models import Fact, Hook, Snapshot, utc_now_iso

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(value: str, fallback: str) -> str:
    return _SLUG_RE.sub("_", value.strip().lower()).strip("_") or fallback


def _is_stub_id(value: str) -> bool:
    low = value.strip().lower()
    return "_stub_" in low or low.startswith("stub_")


def _is_stub_fact(fact: Fact) -> bool:
    return _is_stub_id(fact.fact_id) or "example.test" in (fact.source or "").lower()


def _is_stub_hook(hook: Hook | HookScoreRecord) -> bool:
    return _is_stub_id(hook.hook_id) or any(_is_stub_id(e) for e in hook.evidence)


def _is_stub_snapshot(snapshot: Snapshot) -> bool:
    ident = snapshot.identity
    if ident is not None:
        if "example.test" in (ident.domain or "").lower():
            return True
        if "example.test" in (ident.email or "").lower():
            return True
    if snapshot.facts and all(_is_stub_fact(f) for f in snapshot.facts):
        return True
    return bool(snapshot.hooks and all(_is_stub_hook(h) for h in snapshot.hooks))


def _strip_stub_entries(memory: TargetMemory) -> TargetMemory:
    memory.verified_facts = [f for f in memory.verified_facts if not _is_stub_fact(f)]
    memory.hook_scoreboard = {
        k: v
        for k, v in memory.hook_scoreboard.items()
        if not _is_stub_id(k) and not _is_stub_hook(v)
    }
    return memory


class HookScoreRecord(BaseModel):
    hook_id: str
    topic: str = ""
    evidence: list[str] = Field(default_factory=list)
    why_leverage: str = ""
    confidence: float = 0.5
    touches_touched: int = 0
    positive: int = 0
    score: float = 0.0
    status: str = "candidate"


class ChannelOutcomeRecord(BaseModel):
    sent: int = 0
    replies: int = 0
    rejections: int = 0


class TargetMemory(BaseModel):
    """Cross-session persistent agent memory for one (scenario, target_id)."""

    scenario: str = "practice"
    target_id: str = ""
    target_name: str = ""
    target_org: str = ""
    sessions_seen: int = 0
    updated_at: str = Field(default_factory=utc_now_iso)
    verified_facts: list[Fact] = Field(default_factory=list)
    hook_scoreboard: dict[str, HookScoreRecord] = Field(default_factory=dict)
    watchouts: list[str] = Field(default_factory=list)
    channel_outcomes: dict[str, ChannelOutcomeRecord] = Field(default_factory=dict)
    style_guide: str = ""
    trust_style: str = ""
    wins: list[dict[str, Any]] = Field(default_factory=list)

    def to_strategy_context(self) -> dict[str, Any]:
        """Compact summary for StrategyAgent / ProfileAgent prompt context."""
        return {
            "sessions_seen": self.sessions_seen,
            "known_watchouts": list(self.watchouts[-8:]),
            "channel_outcomes": {
                ch: rec.model_dump(mode="json") for ch, rec in self.channel_outcomes.items()
            },
            "top_hooks": [
                h.model_dump(mode="json")
                for h in sorted(
                    self.hook_scoreboard.values(),
                    key=lambda x: (x.score, x.positive, x.confidence),
                    reverse=True,
                )[:5]
            ],
            "prior_wins": list(self.wins[-3:]),
            "style_guide": self.style_guide,
            "trust_style": self.trust_style,
        }


class TargetMemoryStore:
    """File-backed atomic JSON store for cross-session target memory."""

    def __init__(self, data_dir: Path = Path("data"), *, allow_stub_entries: bool = False) -> None:
        self.base_dir = Path(data_dir) / "target_memory"
        self.allow_stub_entries = allow_stub_entries

    def path_for(self, scenario: str, target_id: str) -> Path:
        sc = _slug(scenario, "practice")
        tid = _slug(target_id, "target")
        return self.base_dir / f"{sc}__{tid}.json"

    def load(self, scenario: str, target_id: str) -> TargetMemory:
        path = self.path_for(scenario, target_id)
        if not path.is_file():
            return TargetMemory(scenario=scenario, target_id=target_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            mem = TargetMemory.model_validate(raw)
            return mem if self.allow_stub_entries else _strip_stub_entries(mem)
        except (OSError, json.JSONDecodeError, ValidationError):
            return TargetMemory(scenario=scenario, target_id=target_id)

    def save(self, memory: TargetMemory) -> Path:
        path = self.path_for(memory.scenario, memory.target_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        memory.updated_at = utc_now_iso()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(memory.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def update_from_snapshot(self, snapshot: Snapshot) -> TargetMemory:
        """Merge verified facts, hook scoreboard, watchouts, channel outcomes, and wins from a run."""
        scenario = snapshot.run.scenario
        target_id = snapshot.target.id or snapshot.run.target_id
        memory = self.load(scenario, target_id)
        if not self.allow_stub_entries and _is_stub_snapshot(snapshot):
            return memory
        memory.target_name = snapshot.target.name or memory.target_name
        memory.target_org = snapshot.target.org or memory.target_org
        memory.sessions_seen += 1

        # 1) Verified active facts (dedup by fact_id or claim)
        facts_by_key: dict[str, Fact] = {
            f.claim.strip().lower(): f for f in memory.verified_facts if f.status == "active"
        }
        for fact in snapshot.facts:
            if fact.status != "active":
                continue
            if not self.allow_stub_entries and _is_stub_fact(fact):
                continue
            if fact.provenance == "inferred" and fact.confidence < 0.6:
                continue
            key = fact.claim.strip().lower()
            existing = facts_by_key.get(key)
            if existing is None or fact.confidence > existing.confidence:
                facts_by_key[key] = fact.model_copy(deep=True)
        memory.verified_facts = list(facts_by_key.values())[:25]

        # 2) Hook scoreboard (merge by topic or hook_id)
        by_topic = {h.topic.strip().lower(): k for k, h in memory.hook_scoreboard.items() if h.topic}
        for hook in snapshot.hooks:
            if not isinstance(hook, Hook):
                continue
            if not self.allow_stub_entries and _is_stub_hook(hook):
                continue
            topic_key = hook.topic.strip().lower()
            slot_key = by_topic.get(topic_key) or hook.hook_id
            prev = memory.hook_scoreboard.get(slot_key)
            if prev is None:
                memory.hook_scoreboard[slot_key] = HookScoreRecord(
                    hook_id=hook.hook_id,
                    topic=hook.topic,
                    evidence=list(hook.evidence),
                    why_leverage=hook.why_leverage,
                    confidence=hook.confidence,
                    touches_touched=hook.engagement.touches_touched,
                    positive=hook.engagement.positive,
                    score=hook.engagement.score,
                    status=hook.status,
                )
            else:
                prev.touches_touched += hook.engagement.touches_touched
                prev.positive += hook.engagement.positive
                prev.score = max(prev.score, hook.engagement.score)
                prev.confidence = max(prev.confidence, hook.confidence)
                if prev.touches_touched >= 2 and prev.positive == 0:
                    prev.status = "exhausted"
                elif hook.status == "active":
                    prev.status = "active"
            if topic_key:
                by_topic[topic_key] = slot_key

        # 3) Watchouts + style/trust guides
        seen_w = {w.strip().lower() for w in memory.watchouts}
        for w in snapshot.profile.watchouts:
            cleaned = w.strip()
            if cleaned and cleaned.lower() not in seen_w:
                memory.watchouts.append(cleaned)
                seen_w.add(cleaned.lower())
        if snapshot.profile.style_guide.strip():
            memory.style_guide = snapshot.profile.style_guide.strip()
        if snapshot.profile.trust_style.strip():
            memory.trust_style = snapshot.profile.trust_style.strip()

        # 4) Channel outcomes from conversation turns
        for turn in snapshot.conversation.turns:
            rec = memory.channel_outcomes.setdefault(turn.channel, ChannelOutcomeRecord())
            if turn.dir == "out":
                rec.sent += 1
            elif turn.dir == "in":
                rec.replies += 1

        # 5) Record wins (dedup by session_id + objective)
        seen_wins = {
            (str(w.get("session_id") or ""), str(w.get("objective") or ""))
            for w in memory.wins
        }
        for inc in snapshot.incidents:
            sig = (str(inc.get("session_id") or ""), str(inc.get("objective") or ""))
            if sig not in seen_wins:
                memory.wins.append(dict(inc))
                seen_wins.add(sig)

        self.save(memory)
        return memory
