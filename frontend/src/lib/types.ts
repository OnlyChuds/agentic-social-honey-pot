export type CampaignState = "BOOT" | "RESEARCH" | "ENGAGE" | "WIN" | "DONE" | "ABORT";

export interface TargetSummary {
  run_id: string;
  scenario: "practice" | "eval";
  target_id: string;
  target_name: string;
  target_role: string;
  target_org: string;
  state: CampaignState;
  touches_used: number;
  session_max: number;
  sessions_used: number;
  sessions_allowed: number;
  latest_suspicion: number | null;
  latest_engagement: number | null;
  latest_mood: string | null;
  profile_confidence: number;
  incidents_count: number;
  sender_name: string;
  sender_domain: string;
  portrait_asset_id: string | null;
  latest_version: number;
  updated_at: string;
  is_running: boolean;
  active_stage?: string | null;
  active_mode?: string | null;
  total_elapsed_ms?: number | null;
  exit_reason?: string | null;
}

export interface TargetListResponse {
  data_dir: string;
  targets: TargetSummary[];
  offline_replays_available?: boolean;
}

export interface FactJSON {
  fact_id: string;
  claim: string;
  category: string;
  source: string;
  provenance: "osint" | "target_claim" | "inferred";
  confidence: number;
  osint_grade: "A" | "B" | "C" | "D" | null;
  status: "active" | "superseded";
  superseded_by: string | null;
  ts: string;
}

export interface HookEngagementJSON {
  touches_touched: number;
  positive: number;
  score: number;
}

export interface HookJSON {
  hook_id: string;
  topic: string;
  evidence: string[];
  why_leverage: string;
  confidence: number;
  engagement: HookEngagementJSON;
  status: "candidate" | "active" | "exhausted";
}

export interface PsychTraitJSON {
  trait: string;
  confidence: number;
  evidence: string[];
}

export interface StressorJSON {
  topic: string;
  confidence: number;
  evidence: string[];
}

export interface ProfileJSON {
  style_guide: string;
  psych: PsychTraitJSON[];
  stressors: StressorJSON[];
  trust_style: string;
  watchouts: string[];
  timezone_hint: string;
  language: string;
  overall_confidence: number;
}

export interface BrandingManifestJSON {
  concept: string;
  sender_name: string;
  title: string;
  company: string;
  real_org: string;
  domain: string;
  email: string;
  phone: string;
  bio: string;
  portrait: { local: string; api_asset_id: string | null };
  logo_svg: string;
  palette: string[];
  fonts: string[];
  signature: string;
  footer_legal: string;
  voice: { tone: string; emoji: string; length: string };
}

export interface AssetMetaJSON {
  asset_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  url: string;
  is_image: boolean;
  exists: boolean;
}

export interface TurnAttachmentJSON {
  kind: "image" | "file" | "virtual";
  asset_id: string | null;
  url: string | null;
  filename: string;
  content_type: string | null;
  exists: boolean;
  transcript: string | null;
}

export interface ConversationTurnJSON {
  n: number;
  ts: string;
  dir: "out" | "in";
  channel: string;
  body: string;
  event_ref: string;
  subject?: string | null;
  facts_used?: string[];
  is_html?: boolean;
  inline_images?: AssetMetaJSON[];
  attachments?: TurnAttachmentJSON[];
}

export interface EventJSON {
  event_id: string;
  ts: string;
  type: string;
  actor: string;
  base_version: number;
  payload: Record<string, unknown>;
}

export interface DecisionActionJSON {
  op: string;
  [key: string]: unknown;
}

export interface DecisionRecordJSON {
  ts: string;
  trigger: string;
  snapshot_version: number;
  actions: DecisionActionJSON[];
  notes: string;
}

export interface RegisteredPageJSON {
  session_id: string;
  page_id: string;
  url: string;
  title: string;
  fields: string[];
  created_at: string;
}

export interface SnapshotJSON {
  version: number;
  run: {
    scenario: "practice" | "eval";
    target_id: string;
    run_no: number;
    state: CampaignState;
  };
  target: {
    id: string;
    name: string;
    role: string;
    org: string;
  };
  facts: FactJSON[];
  hooks: HookJSON[];
  profile: ProfileJSON;
  identity: BrandingManifestJSON | null;
  campaign: {
    plan: {
      objective: string;
      primary_hook: string;
      touches: Array<{
        n: number;
        channel: string;
        intent: string;
        hook: string;
        escalation: number;
        stop_if: string;
      }>;
      landing_page: {
        pretext: string;
        fields: string[];
        trigger_touch: number;
        url_path: string;
      } | null;
      action_artifact: Record<string, unknown> | null;
      pivot_triggers: string[];
      give_up: string;
      session_restart: { recommended: boolean; reason: string | null };
    } | null;
    budget: { session_max: number; used: number };
  };
  sessions: { allowed: number; used: number; current: string | null };
  conversation: { session_id: string | null; turns: ConversationTurnJSON[] };
  incidents: Array<Record<string, unknown>>;
}

export interface StageTransition {
  stage: CampaignState;
  from_stage: string | null;
  ts: string;
  version: number;
  reason: string;
}

export interface SignalPoint {
  seq: number;
  touch_n: number;
  version: number;
  ts: string;
  channel: string;
  engagement: number;
  suspicion: number;
  mood: string;
  hook_hits: string[];
  proposed_next: string;
  reason: string;
}

export interface TimelineMarker {
  version: number;
  touch_n: number;
  ts: string;
  kind:
    | "pivot"
    | "new_session"
    | "register_page"
    | "link.opened"
    | "page.submitted"
    | "attachment.opened"
    | "incident";
  label: string;
}

export interface TraitEvolutionItem {
  trait: string;
  initial_confidence: number | null;
  confidence: number;
  delta: number;
  is_new: boolean;
  evidence: string[];
}

export interface PersonalityMilestone {
  version: number;
  ts: string;
  touch_n: number;
  label: string;
  state: CampaignState;
  profile: ProfileJSON;
  traits: TraitEvolutionItem[];
  hooks: HookJSON[];
  watchouts_added: string[];
  suspicion: number | null;
  engagement: number | null;
  mood: string | null;
}

export interface WatchoutEntry {
  text: string;
  first_seen_version: number;
  first_seen_touch: number;
  ts: string;
}

export interface ActiveStageStep {
  step_id: string;
  stage: string;
  mode: string;
  started_at: string;
  elapsed_ms: number;
}

export interface CompletedStageStep {
  step_id: string;
  stage: string;
  mode: string;
  started_at: string;
  finished_at: string;
  elapsed_ms: number;
  ok: boolean;
  inferred?: boolean;
}

export interface StageTimingSummary {
  stage: string;
  calls: number;
  failures: number;
  total_ms: number;
  avg_ms: number;
  max_ms: number;
  last_ms: number;
}

export interface ExecutionStatus {
  is_running: boolean;
  started_at: string | null;
  finished_at: string | null;
  total_elapsed_ms: number;
  exit_reason: string | null;
  active_steps: ActiveStageStep[];
  completed_steps: CompletedStageStep[];
  stage_totals: StageTimingSummary[];
}

export interface TargetDetailResponse {
  run_id: string;
  summary: TargetSummary;
  execution_status?: ExecutionStatus;
  latest_snapshot: SnapshotJSON;
  stage_history: StageTransition[];
  signal_series: SignalPoint[];
  event_markers: TimelineMarker[];
  personality_evolution: PersonalityMilestone[];
  watchout_history: WatchoutEntry[];
  turns: ConversationTurnJSON[];
  events: EventJSON[];
  decisions: DecisionRecordJSON[];
  registered_pages: RegisteredPageJSON[];
  facts_by_id: Record<string, FactJSON>;
  assets_by_id?: Record<string, AssetMetaJSON>;
}

export interface LaunchRunRequest {
  target_id: string;
  mode: "practice_live";
  stub_objective?: "credentials" | "action" | "disclosure";
  zero_pacing: boolean;
  aggressive: boolean;
}

export interface LaunchRunResponse {
  run_id: string;
  target_id: string;
  mode: string;
  pid: number;
  status: string;
  db_path: string;
}
