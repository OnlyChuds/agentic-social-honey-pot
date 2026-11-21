from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


EventType = Literal[
    "target_event",
    "agent_output",
    "orchestrator_decision",
    "incident",
    "session",
    "system",
]

Actor = Literal[
    "target",
    "osint",
    "profile",
    "identity",
    "messaging",
    "strategy",
    "qa",
    "orchestrator",
    "system",
]

ProposalKind = Literal[
    "facts",
    "dossier",
    "profile_update",
    "hook_update",
    "identity",
    "message",
    "signals",
    "qa_verdict",
    "plan",
]

FactCategory = Literal[
    "role",
    "projects",
    "talks",
    "company",
    "tech",
    "social",
    "relationship",
    "other",
]

Provenance = Literal["osint", "target_claim", "inferred"]
OsintGrade = Literal["A", "B", "C", "D"]
FactStatus = Literal["active", "superseded"]
HookStatus = Literal["candidate", "active", "exhausted"]
CampaignState = Literal["BOOT", "RESEARCH", "ENGAGE", "WIN", "ABORT", "DONE"]
Objective = Literal["credentials", "action", "disclosure", "your_call"]

Channel = Literal[
    "email",
    "calendar_invite",
    "shared_document",
    "voicemail",
    "sms",
    "whatsapp",
    "signal",
    "telegram",
    "imessage",
    "slack",
    "teams_chat",
    "discord",
    "linkedin_message",
    "linkedin_connection_request",
    "x_dm",
    "instagram_dm",
    "facebook_messenger",
    "mfa_push",
    "phone_call",
    "video_call",
    "teams_call",
    "live_chat",
]


# --- 8.1 Event log ---


class Event(BaseModel):
    event_id: str = Field(default_factory=lambda: make_id("e"))
    ts: str = Field(default_factory=utc_now_iso)
    type: EventType
    actor: Actor
    base_version: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)


# --- 8.2 Agent proposal ---


class Proposal(BaseModel):
    proposal_id: str = Field(default_factory=lambda: make_id("p"))
    agent: Actor
    kind: ProposalKind
    base_version: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""


# --- 8.3 Fact ---


class Fact(BaseModel):
    fact_id: str = Field(default_factory=lambda: make_id("f"))
    claim: str
    category: FactCategory = "other"
    source: str = ""
    provenance: Provenance = "osint"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    osint_grade: OsintGrade | None = None
    status: FactStatus = "active"
    superseded_by: str | None = None
    ts: str = Field(default_factory=utc_now_iso)


# --- 8.4 Hook ---


class HookEngagement(BaseModel):
    touches_touched: int = 0
    positive: int = 0
    score: float = 0.0


class Hook(BaseModel):
    hook_id: str = Field(default_factory=lambda: make_id("h"))
    topic: str
    evidence: list[str] = Field(default_factory=list)
    why_leverage: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    engagement: HookEngagement = Field(default_factory=HookEngagement)
    status: HookStatus = "candidate"


# --- 8.5 Profile ---


class PsychTrait(BaseModel):
    trait: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class Stressor(BaseModel):
    topic: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class Profile(BaseModel):
    style_guide: str = ""
    psych: list[PsychTrait] = Field(default_factory=list)
    stressors: list[Stressor] = Field(default_factory=list)
    trust_style: str = ""
    watchouts: list[str] = Field(default_factory=list)
    timezone_hint: str = "Europe/Berlin"
    language: str = "en"
    overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


# --- 8.6 Branding manifest ---


class PortraitAsset(BaseModel):
    local: str = ""
    api_asset_id: str | None = None


class VoiceStyle(BaseModel):
    tone: str = "warm professional"
    emoji: str = "none"
    length: str = "2-4 sentences"


class BrandingManifest(BaseModel):
    concept: str = ""
    sender_name: str = ""
    title: str = ""
    company: str = ""
    real_org: str = ""
    domain: str = ""
    email: str = ""
    phone: str = ""
    bio: str = ""
    portrait: PortraitAsset = Field(default_factory=PortraitAsset)
    logo_svg: str = ""
    palette: list[str] = Field(default_factory=list)
    fonts: list[str] = Field(default_factory=lambda: ["Inter", "system-ui"])
    signature: str = ""
    footer_legal: str = ""
    voice: VoiceStyle = Field(default_factory=VoiceStyle)


# --- 8.7 Campaign plan ---


class TouchSpec(BaseModel):
    n: int
    channel: Channel
    intent: str
    hook: str = ""
    escalation: int = 1
    stop_if: str = ""


class LandingPageSpec(BaseModel):
    pretext: str
    fields: list[str] = Field(default_factory=list)
    trigger_touch: int = 5
    url_path: str = "/portal"


class SessionRestartSpec(BaseModel):
    recommended: bool = False
    reason: str | None = None


class CampaignPlan(BaseModel):
    objective: Objective = "credentials"
    primary_hook: str = ""
    touches: list[TouchSpec] = Field(default_factory=list)
    landing_page: LandingPageSpec | None = None
    action_artifact: dict[str, Any] | None = None
    pivot_triggers: list[str] = Field(default_factory=list)
    give_up: str = ""
    session_restart: SessionRestartSpec = Field(default_factory=SessionRestartSpec)


# --- QA Verdict ---


class QAIssue(BaseModel):
    criterion: str
    severity: Literal["critical", "major", "minor"]
    fix: str


class QAVerdict(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    passed: bool = Field(alias="pass")
    issues: list[QAIssue] = Field(default_factory=list)
    required_fixes: list[str] = Field(default_factory=list)
    optional_fixes: list[str] = Field(default_factory=list)


# --- 8.8 Snapshot ---


class RunMeta(BaseModel):
    scenario: Literal["practice", "eval"] = "practice"
    target_id: str = ""
    run_no: int = 1
    state: CampaignState = "BOOT"


class TargetInfo(BaseModel):
    id: str = ""
    name: str = ""
    role: str = ""
    org: str = ""


class TouchBudget(BaseModel):
    session_max: int = 30
    used: int = 0


class CampaignView(BaseModel):
    plan: CampaignPlan | None = None
    budget: TouchBudget = Field(default_factory=TouchBudget)


class SessionBudget(BaseModel):
    allowed: int = 3
    used: int = 0
    current: str | None = None


class ConversationTurn(BaseModel):
    n: int
    ts: str = Field(default_factory=utc_now_iso)
    dir: Literal["out", "in"]
    channel: Channel
    body: str
    event_ref: str = ""


class ConversationView(BaseModel):
    session_id: str | None = None
    turns: list[ConversationTurn] = Field(default_factory=list)


class Snapshot(BaseModel):
    version: int = 0
    run: RunMeta = Field(default_factory=RunMeta)
    target: TargetInfo = Field(default_factory=TargetInfo)
    facts: list[Fact] = Field(default_factory=list)
    hooks: list[Hook] = Field(default_factory=list)
    profile: Profile = Field(default_factory=Profile)
    identity: BrandingManifest | None = None
    campaign: CampaignView = Field(default_factory=CampaignView)
    sessions: SessionBudget = Field(default_factory=SessionBudget)
    conversation: ConversationView = Field(default_factory=ConversationView)
    incidents: list[dict[str, Any]] = Field(default_factory=list)


# --- 8.9 Orchestrator decision record ---


class DecisionAction(BaseModel):
    model_config = ConfigDict(extra="allow")

    op: Literal[
        "commit",
        "reject",
        "trigger",
        "send",
        "register_page",
        "declare_incident",
        "new_session",
    ]


class DecisionRecord(BaseModel):
    ts: str = Field(default_factory=utc_now_iso)
    trigger: Literal["target_event", "pacing", "agent_output", "boot"]
    snapshot_version: int = 0
    actions: list[DecisionAction] = Field(default_factory=list)
    notes: str = ""
