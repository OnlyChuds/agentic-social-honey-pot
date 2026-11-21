from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from src.models import Channel, Snapshot

ClockFn = Callable[[], float]

VISUAL_CHANNELS: frozenset[str] = frozenset({"email"})
LIVE_CALL_CHANNELS: frozenset[str] = frozenset(
    {"phone_call", "video_call", "teams_call", "live_chat"}
)


@dataclass(slots=True)
class PolicyState:
    """Per-session counters enforced by TriggerPolicy."""

    consecutive_no_reply: int = 0
    events_since_profile: int = 0
    osint_refreshes_this_session: int = 0
    last_channel: str | None = None
    same_channel_streak: int = 0
    recent_engagements: list[float] = field(default_factory=list)
    last_engagement: float = 0.0
    last_suspicion: float = 0.0
    last_touch_monotonic: float | None = None

    def reset_for_new_session(self) -> None:
        self.consecutive_no_reply = 0
        self.events_since_profile = 0
        self.osint_refreshes_this_session = 0
        self.last_channel = None
        self.same_channel_streak = 0
        self.recent_engagements.clear()
        self.last_engagement = 0.0
        self.last_suspicion = 0.0
        self.last_touch_monotonic = None


class TriggerPolicy:
    """Deterministic budget, pacing, and anti-storm trigger policy."""

    def __init__(
        self,
        *,
        min_gap_s: float = 600.0,
        max_revisions: int = 2,
        max_osint_refreshes_per_session: int = 1,
        profile_event_batch: int = 2,
        suspicion_pivot_threshold: float = 0.6,
        no_reply_pivot_threshold: int = 2,
        clock: ClockFn = time.monotonic,
    ) -> None:
        self.min_gap_s = max(0.0, float(min_gap_s))
        self.max_revisions = max_revisions
        self.max_osint_refreshes_per_session = max_osint_refreshes_per_session
        self.profile_event_batch = profile_event_batch
        self.suspicion_pivot_threshold = suspicion_pivot_threshold
        self.no_reply_pivot_threshold = no_reply_pivot_threshold
        self._clock = clock
        self.state = PolicyState()

    def reset_for_new_session(self) -> None:
        self.state.reset_for_new_session()

    # --- Budget & Session Guards ---

    @staticmethod
    def can_spend_touch(snapshot: Snapshot) -> bool:
        if snapshot.run.state in {"WIN", "ABORT", "DONE"}:
            return False
        if snapshot.incidents:
            return False
        return snapshot.campaign.budget.used < snapshot.campaign.budget.session_max

    @staticmethod
    def remaining_touches(snapshot: Snapshot) -> int:
        return max(0, snapshot.campaign.budget.session_max - snapshot.campaign.budget.used)

    @staticmethod
    def can_start_new_session(snapshot: Snapshot) -> bool:
        if snapshot.run.state in {"WIN", "ABORT"}:
            return False
        return snapshot.sessions.used < snapshot.sessions.allowed

    # --- Pacing ---

    def pacing_wait_seconds(self) -> float:
        if self.min_gap_s <= 0.0 or self.state.last_touch_monotonic is None:
            return 0.0
        elapsed = self._clock() - self.state.last_touch_monotonic
        return max(0.0, self.min_gap_s - elapsed)

    def record_touch_sent(self, channel: Channel, *, had_reply: bool, event_count: int) -> None:
        self.state.last_touch_monotonic = self._clock()
        if channel == self.state.last_channel:
            self.state.same_channel_streak += 1
        else:
            self.state.last_channel = channel
            self.state.same_channel_streak = 1

        if had_reply:
            self.state.consecutive_no_reply = 0
        else:
            self.state.consecutive_no_reply += 1

        self.state.events_since_profile += event_count

    def record_signal(self, *, engagement: float, suspicion: float) -> None:
        self.state.last_engagement = engagement
        self.state.last_suspicion = suspicion
        self.state.recent_engagements.append(engagement)
        if len(self.state.recent_engagements) > 5:
            self.state.recent_engagements = self.state.recent_engagements[-5:]

    def mark_profile_ran(self) -> None:
        self.state.events_since_profile = 0

    def mark_osint_refreshed(self) -> None:
        self.state.osint_refreshes_this_session += 1

    # --- Trigger Decisions ---

    def should_trigger_profile(self) -> bool:
        return self.state.events_since_profile >= self.profile_event_batch

    def should_trigger_osint_refresh(
        self,
        snapshot: Snapshot,
        *,
        explicit_request: bool = False,
    ) -> bool:
        if self.state.osint_refreshes_this_session >= self.max_osint_refreshes_per_session:
            return False
        if explicit_request:
            return True
        return snapshot.profile.overall_confidence < 0.5

    def _engagement_is_declining(self) -> bool:
        vals = self.state.recent_engagements
        if len(vals) < 3:
            return False
        return vals[-3] > vals[-2] > vals[-1]

    def strategy_pivot_reason(self, snapshot: Snapshot) -> str | None:
        if snapshot.campaign.plan is None:
            return "no active campaign plan"
        if self.state.consecutive_no_reply >= self.no_reply_pivot_threshold:
            return (
                f"{self.state.consecutive_no_reply} consecutive touches without reply "
                f"(threshold={self.no_reply_pivot_threshold})"
            )
        if self.state.last_suspicion >= self.suspicion_pivot_threshold:
            return (
                f"target suspicion {self.state.last_suspicion:.2f} >= "
                f"{self.suspicion_pivot_threshold:.2f}"
            )
        if self.state.same_channel_streak >= 3 and self._engagement_is_declining():
            return (
                f"{self.state.same_channel_streak} same-channel touches "
                f"({self.state.last_channel}) with declining engagement"
            )
        return None

    def should_trigger_strategy(self, snapshot: Snapshot) -> bool:
        return self.strategy_pivot_reason(snapshot) is not None

    @staticmethod
    def needs_qa(channel: str, body: str, *, is_landing_page: bool = False) -> bool:
        if is_landing_page:
            return True
        if channel in VISUAL_CHANNELS:
            lowered = body.lower()
            return "<html" in lowered or "<div" in lowered or "<table" in lowered or "<img" in lowered
        return False

    @staticmethod
    def is_live_channel(channel: str) -> bool:
        return channel in LIVE_CALL_CHANNELS
