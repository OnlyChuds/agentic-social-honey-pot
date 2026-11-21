export type CampaignState = "BOOT" | "RESEARCH" | "ENGAGE" | "WIN" | "DONE" | "ABORT";

export interface TargetSummary {
  run_id: string;
  scenario: string;
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
}

export interface TargetListResponse {
  data_dir: string;
  targets: TargetSummary[];
}

export interface StageTransition {
  stage: CampaignState;
  from_stage: CampaignState | null;
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
  kind: string;
  label: string;
}

export interface WatchoutEntry {
  text: string;
  first_seen_version: number;
  first_seen_touch: number;
  ts: string;
}

export interface FactJSON {
  fact_id: string;
  claim: string;
  category: string;
  source: string;
  provenance: string;
  confidence: number;
  osint_grade: string | null;
  status: string;
  ts?: string;
}

export interface PsychTraitJSON {
  trait: string;
  confidence: number;
  evidence: string[];
}

export interface StressorJSON {
  topic: string;
  confidence: number;
  evidence?: string[];
}

export interface ProfileJSON {
  psych: PsychTraitJSON[];
  style_guide: string;
  trust_style: string;
  watchouts: string[];
  stressors: StressorJSON[];
  overall_confidence: number;
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
  status: string;
}

export interface TraitEvolutionEntry {
  trait: string;
  initial_confidence: number;
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
  traits: TraitEvolutionEntry[];
  hooks: HookJSON[];
  watchouts_added: string[];
  suspicion: number | null;
  engagement: number | null;
  mood: string | null;
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

export interface NormalizedAttachmentJSON {
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
  dir: "out" | "in";
  channel: string;
  body: string;
  ts: string;
  event_ref: string;
  subject?: string | null;
  facts_used?: string[];
  is_html?: boolean;
  inline_images?: AssetMetaJSON[];
  attachments?: NormalizedAttachmentJSON[];
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
  snapshot_version: number;
  trigger: string;
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
    scenario: string;
    target_id: string;
    state: CampaignState;
  };
  facts: FactJSON[];
  hooks: HookJSON[];
  profile: ProfileJSON;
  [key: string]: unknown;
}

export interface TargetDetailResponse {
  run_id: string;
  summary: TargetSummary;
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
  assets_by_id: Record<string, AssetMetaJSON>;
}

export interface LaunchRunRequest {
  target_id: string;
  mode: "mock_stub" | "practice_live";
  stub_objective: "credentials" | "action" | "disclosure";
  zero_pacing: boolean;
  aggressive: boolean;
  scenario?: string;
}

export interface LaunchRunResponse {
  run_id: string;
  scenario: string;
  target_id: string;
  run_no: number;
  mode: string;
  stub_objective: string | null;
  pid: number;
  db_path: string;
  started_at: string;
  status: string;
  returncode: number | null;
}
