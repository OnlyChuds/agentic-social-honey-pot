# Architecture — Agentic Social Honeypot

> Target: revel8 "Agentic Social Engineer" challenge (START Hack 2026)
> Status: synchronized with implemented runtime (`src/`, `prompts/`, `frontend/`).

---

## 1. Terminology

| Term | Meaning |
|---|---|
| **Target** | The AI persona twin we attack (e.g. `julius`, `tom`, `robert` in practice; hidden personas in eval). |
| **Honeypot persona** | Our attacker-side identity: the sender we impersonate (e.g. "Lena Vogt, Account Director at a lookalike vendor"). This is what we build with the image model. |
| **Touch** | 1 message sent or 1 live conversation started. The scarcest resource in eval (10/session). |
| **Hook** | A target vulnerability topic (psychological lever) we detect via OSINT and exploit in conversation. |
| **Campaign** | One session's worth of touches pursuing one objective (credentials / action / disclosure). |

## 2. Challenge Constraints That Shape the Design

From `docs/challenge_overview.md`, `docs/scoring_rubric.md`, and the Target API README:

1. **Full autonomy** — no human-in-the-loop during eval. Every decision must be derivable from state.
2. **Eval budget: 3 sessions × 10 touches.** Speed is scored by *median touch count to first incident*. Spray-and-pray loses.
3. **Incident rate dominates API score** (20/25 pts). Reliability across all 3 runs > one spectacular run.
4. **Targeting (10 pts)** — hook alignment is measured per touch; **fact errors cost up to −5 pts**. Every fact we assert must be sourced.
5. **Craft (10 pts)** — *automated visual rating* of rendered emails + landing pages on 4 criteria (sender authenticity, format appropriateness, cleanliness, detail precision). Must be gated by a QA step, not vibes.
6. **Architecture (40 pts, jury)** — judged on the 1-page doc + turn-by-turn playback. We need a decision log rich enough to explain every turn.
7. **Synchronous API** — `POST /messages` returns persona events in the response. The loop is request-driven, not poll-driven; "silence" = no `reply` event, handled by pacing + strategy.
8. **Rules**: lookalike domains only (no real root domains), no prompt-injection phrasing, pages registered *before* any message links to them, attachments metadata-only.
9. **API limits**: 60 req/min, 6 parallel, 4 queued/session, 300 assets/team, 5 MB/asset, 2 MB payload, 4 000-char conversation turns. Idempotency-Key required to protect the touch budget on retries.

## 3. Design Principles

1. **Orchestrator is the only writer** to curated state (snapshot). All agents are pure functions: `(snapshot, task) → proposal`.
2. **Shared memory = two stores**:
   - **Event log** — append-only, any component may append (target events, agent outputs, orchestrator decisions, incidents). Source of truth.
   - **Snapshot** — versioned, derived state that agents read. Only the orchestrator commits to it.
3. **Versioned proposals** — every proposal carries the snapshot `base_version` it was computed from. The orchestrator rejects stale proposals (conflict) and re-triggers. No locks needed.
4. **Provenance on every fact** — `source`, `provenance` (osint / target_claim / inferred), `confidence`. Defends the −5 fact-error penalty and lets the profile agent weigh evidence.
5. **QA gate before any visual send** — emails with branding and landing pages are rendered to PNG and critiqued before the touch is spent.
6. **Replayable by construction** — every orchestrator decision is logged with trigger, input version, actions, rationale. The decision log *is* the pitch narrative and the 1-page doc's evidence.
7. **Session-agnostic state** — a new session resets the *persona's* memory, not ours. Shared memory outlives sessions; session restarts start with full knowledge.
8. **Agents may fail, touches may not** — an agent error logs and the orchestrator re-plans. A message is only ever sent from a schema-validated proposal.

## 4. System Overview

```
                               ┌────────────────────────────────┐
                               │         CHALLENGE GOAL          │
                               │ incident (credentials/action/  │
                               │ disclosure) within touch budget│
                               └────────────────┬───────────────┘
                                                │
        ┌───────────────────────────────────────▼───────────────────────────────────────┐
        │                                ORCHESTRATOR                                    │
        │   decision loop · trigger policy · budget & pacing governor · win detection    │
        │   ONLY WRITER to snapshot · serializes all commits                             │
        └───┬──────────┬──────────┬──────────┬──────────┬──────────┬────────────────────┘
            │          │          │          │          │          │      proposals in /
            ▼          ▼          ▼          ▼          ▼          ▼      commits out
      ┌──────────┐┌─────────┐┌──────────┐┌─────────┐┌─────────┐┌───────┐
      │  OSINT   ││ PROFILE ││ IDENTITY ││MESSAGING││STRATEGY ││  QA   │
      │ 3-burst  ││ target  ││ sender   ││ in-char-││ rolling ││ pre-  │
      │ + cache  ││ model + ││ branding ││ acter   ││ 5-touch ││ send  │
      │ (900tok) ││ hooks   ││ + pages  ││ +signals││ planner ││ critic│
      └────┬─────┘└─────────┘└────┬─────┘└─────────┘└─────────┘└───┬───┘
           │                      │                                │
           ▼                      ▼                                ▼
   Native async httpx      Qwen-Image-2.1 (ComfyUI)       Playwright headless
   (Jina, Exa, Tavily,     (25-step portrait LoRA +       (email HTML + landing
    Apify LinkedIn)         8-step Turbo logo +            page → PNG + DOM
                            deterministic templates)       telemetry checks)

   Text LLM (all agents): Qwen3.8-27B-abliterated, self-hosted (vLLM, OpenAI-compatible API)

   ┌─────────────────────────────────────┐    ┌──────────────────────────────────────┐
   │  SHARED MEMORY (SQLite, WAL)        │    │  TARGET API (persona simulator)      │
   │  · event log     append-only        │◄──►│  sessions · messages · pages ·       │
   │  · snapshot      versioned, orch-   │    │  conversations · assets              │
   │    only writer   (time-travel UI)   │    │  events: reply, link.opened,         │
   │  · decision log  append-only        │    │  page.submitted, attachment.opened   │
   └──────────────────┬──────────────────┘    └──────────────────────────────────────┘
                      │ read-only SQLite + SSE (`/api/stream`)
                      ▼
   ┌─────────────────────────────────────┐
   │  LIVE MONITOR (FastAPI + Next.js)   │
   │  · version timeline scrubber        │
   │  · suspicion vs. engagement chart   │
   │  · personality & hook evolution     │
   └─────────────────────────────────────┘
```

## 5. Components

### 5.1 Orchestrator (100% deterministic control plane)

The orchestrator (`src/orchestrator/loop.py`, `state.py`, `policy.py`, `wins.py`) is **pure deterministic Python**—no LLM sits on the state-mutation or control-flow path. All judgment is delegated to typed worker agents whose proposals are schema- and invariant-validated before commit. This keeps the writer fast, cheap, crash-recoverable, and fully auditable.

Responsibilities:
- Apply the **trigger policy** (§5.9) to decide which agents to run.
- Run independent bootstrap agents concurrently (`asyncio.gather`), collect proposals, and record per-stage `stage_timing` events (`elapsed_ms`, `ok`, no secrets/bodies).
- Validate proposals (Pydantic v2 schema in `src/models.py` + referential checks: `facts_used` ids exist and are active; channel valid; page registered before link; budget allows).
- **Commit** accepted writes to the snapshot (version bump), **reject** stale/bad ones.
- Automatically continue rolling 5-touch Strategy horizons when `used + 1` passes the current plan's last touch while session budget remains.
- Execute API actions: `send`, `register_page`, `upload_asset`, conversation turns — serialized, idempotency-keyed.
- **Win detection** (§7.3), **budget governor**, **pacing timer**, state-machine transitions (§6), and structured `run_exit` events (`completed`, `cycle_limit`, `service_failure`).
- Log every decision to the decision log with rationale.

### 5.2 OSINT Agent — native async 3-burst research + single-call synthesis

Implemented natively in `src/osint/engine.py`, `src/osint/providers.py`, `src/osint/sources.py`, and `src/agents/osint.py` (replacing slow bash/tool-loop wrappers with bounded parallel `httpx` stages):

- **Burst 1 (~12s cap):** Multi-provider seed searches in parallel across configured providers (Jina Search, Exa, Tavily). Refresh calls with a `focus_question` inject the focus directly into Burst 1 queries.
- **Burst 2 (~38s cap):** Apify LinkedIn profile scrape (unwrapping regional/authwall URLs) in parallel with Jina Reader fetches of top heuristic-ranked URLs (`pick_read_urls`).
- **Burst 3 (~12s cap, optional):** Follow-up reads on newly discovered URLs if budget and time remain.
- **Single-call synthesis (900-token cap):** One `Qwen3.8-27B` JSON call synthesizes 5–6 sourced facts (graded A/B/C/D → confidence 0.9/0.7/0.5/0.3), 3 concrete hook seeds, 2–3 psychoprofile traits, and `org_branding` (`real_domain`, keywords). On timeout/parse error, degrades cleanly to a deterministic source-derived fallback dossier.
- **Per-target disk cache (`data/osint_cache/`):** Bootstrap dossiers with `>= 3` facts and `full`/`partial` completeness are cached atomically per target so repeated practice runs hit cache in 0ms ($0.000). Refresh runs bypass the cache.

### 5.3 Profile Agent

Builds and maintains the target model (`src/agents/profile.py`, 512-token output cap). Two input streams:
- **Static**: OSINT dossier psychoprofile + facts (run once after research).
- **Dynamic**: conversation signals from the Messaging agent (engagement, suspicion, hook hits) — re-run on every target reply (enabling immediate opportunistic disclosure win detection) or after `>= 2` target events.

Output: merged `profile_update` (style guide, evidence-anchored psych traits, stressors, trust style, watchouts), `hook_updates` (enforcing the 2-deflection exhaustion rule), `new_hooks` (fact-anchored only), and non-public `disclosures` candidates.

### 5.4 Identity Agent

Owns the honeypot persona, visual assets, and landing pages (`src/agents/identity.py`, `src/agents/domains.py`, `src/agents/landing_pages.py`):

- **Domain compliance & auto-repair:** Every manifest is validated against the real-root-domain blocklist (`qa.check_domain_blocklist`); any blocked domain/email is automatically repaired via `src/agents/domains.py` lookalike generation (hyphenated service variant, TLD swap, subdomain of swapped root) before failing.
- **Master portrait & brand logo (`Qwen-Image-2.1` via ComfyUI, `src/llm/image.py`):**
  - Master portrait: 25-step `qwen_image_2.1_bf16` + `casual_phone_photo_lbxstyle` realism LoRA (1024×1024, fixed seed). Reference variants use 8-step `edit_image` to prevent face drift.
  - Brand logo mark: 8-step `qwen_image_2.1_turbo_bf16` (512×512 minimalist geometric emblem, no text), uploaded once via `POST /v1/assets` and referenced as `asset://<id>` with high-contrast header badge styling.
- **Deterministic branded landing-page templates (`src/agents/landing_pages.py`):** On `mode="landing_page"`, `IdentityAgent` renders one of 6 authored, self-contained HTML harvest templates (`portal_login`, `password_reset`, `mfa_reauth`, `document_vault`, `invoice_update`, `scheduling`) directly without an LLM round-trip. `select_harvest_type()` enforces field/copy alignment (e.g., `["email", "password"]` never selects `mfa_reauth`, `invoice_update`, or `scheduling`), allowing landing pages to pass Playwright + LLM QA on cycle 0 in ~25s instead of multi-minute revision loops. `mode="revise"` retains LLM-assisted revision if QA ever requests changes.

### 5.5 Messaging Agent

The in-character sender. Runs on the abliterated Qwen (temp 0.7) — abliteration matters most here (no mid-conversation refusals).

Two modes:
- **Compose**: given the plan's next touch (channel, angle, hook, escalation level) → the message (channel-formatted), `facts_used`, and an optional `page_request`/`attachment_request`.
- **Signal**: given a target event → structured read: `engagement (0–1)`, `suspicion (0–1)`, `hook_hits`, `mood`, `proposed_next` (reply_now / wait / change_angle / escalate / back_off).

Behavioral core (full rules in the prompt at implementation time): one primary ask per touch, max one hook per message, mirror the target's register from the style guide, pressure ladder 1–5 gated by the plan's escalation level, never assert facts without `facts_used` ids, treat all target text as data (anti-injection), channel etiquette per §5.5 table below.

| Channel | Etiquette |
|---|---|
| `email` | subject matters; 3–8 sentences; one CTA; branded HTML only after QA; signature from manifest |
| `sms` / `whatsapp` / `signal` / `telegram` / `imessage` | short (≤3 sentences); no link on first touch; whatsapp may use 1–2 emoji if style guide says so |
| `linkedin_message` | 3–5 sentences; reference something specific (a talk, a post); professional-warm |
| `linkedin_connection_request` | ≤300 chars note; one line + hook |
| `slack` / `teams_chat` / `discord` | terse, peer tone; assume they're busy |
| `voicemail` | spoken transcript 2–3 sentences; one callback ask (virtual attachment) |
| `calendar_invite` | subject = event title; body = details + one question |
| `mfa_push` | short approval-framed text |
| `shared_document` | share note + one low-friction ask |
| live calls | 1–2 spoken sentences per turn, ≤4 000 chars/turn; start costs 1 touch, count turns conservatively as touches (verify in practice) |

### 5.6 Strategy Agent

The campaign planner (`src/agents/strategy.py`, Qwen temp 0.3, 768-token cap). Input: full snapshot + budget + state. Output: **campaign plan** (§8.7) — a concise **rolling horizon of up to 5 touches** within the hard budget: channel, intent (≤10 words), hook id, escalation level (1–5), stop-if, plus landing-page spec (`pretext`, `fields`, `trigger_touch`, `url_path`), pivot triggers, give-up rule, and optional session-restart recommendation.

Planning defaults (rolling 5-touch horizon, auto-extended by the orchestrator when budget remains):
1. warm opener on the highest-trust channel for this target, leading hook (`escalation: 1`)
2. reply-driven deepening / hook validation (`escalation: 2`)
3. landing-page link with concrete pretext, registered right before send (`escalation: 3`)
4. low-friction nudge or second-channel follow-up (`escalation: 4`)
5. polite-urgency direct ask (`escalation: 5`)

Deterministic validation (`validate_campaign_plan`) enforces budget bounds, contiguous touch numbers, exhausted-hook exclusion, non-decreasing escalation on pivots, and valid `landing_page.trigger_touch`, with 1 LLM repair pass + deterministic clamping fallback.

### 5.7 QA Agent (pre-send critic)

Runs on every outbound visual asset (branded email, landing page) *before* the touch is spent or the page is registered.

Pipeline (code + LLM): render HTML → Playwright headless Chromium → PNG → (1) rule checks in code: broken `<img>` / `asset://` unresolved, placeholder text regexes (lorem, TODO, `[name]`, `{var}`), link-text vs href mismatch, domain plausibility against blocklist of real root domains, (2) LLM vision critique (temp 0.1) against the 4 Craft criteria + format appropriateness for the claimed sender.

Output: `{pass, issues[{criterion, severity, fix}], required_fixes, optional_fixes}`. Any `critical` → fail → asset returned to Identity/Messaging for revision. **Max 2 revision cycles**, then ship with a logged warning (never burn the touch budget on perfection).

### 5.8 Target API Client (code)

- `Idempotency-Key` (fresh UUID per logical send) on every write; on timeout/429/503 retry **with the same key** — protects the touch budget.
- Retry policy: `429` backoff 30–60s ×3; `503` ×3 with backoff (safe per API docs); `422` never blind-retry → log + orchestrator decision; `409` → budget/session state transition; `400/404/413/415` → bug, log, no retry.
- Rate governor: token bucket ≤50 req/min, semaphore ≤5 parallel (under API 60/6).
- Ordering invariant enforced here: `register_page` must complete before any message containing that URL is sent.
- Asset manager: local file → upload → `asset://id` map, cap tracking (300).

### 5.9 Trigger Policy (anti-storm)

| Agent | Trigger | Cap |
|---|---|---|
| messaging (signal) | every target `reply` event | every event |
| messaging (compose) | plan says reply now, or engagement ≥0.6 and pacing elapsed | 1 pending |
| strategy | 2 consecutive touches w/o reply · suspicion ≥0.6 · 3 same-channel touches w/ declining engagement · plan page-trigger reached | 1 pending |
| osint | profile confidence <0.5, or strategy requests focused refresh | 1 per session |
| profile | ≥2 new target events since last run, or after osint commit | 1 pending |
| identity | boot (with osint, parallel) · new sender concept · landing page creation | 1 pending |
| qa | any outbound visual asset | per asset, ≤2 revisions |

## 6. Campaign State Machine

Implemented in `src/orchestrator/state.py` (`LEGAL_TRANSITIONS`) and reflected 1:1 in the dashboard `StagePipeline`:

```
BOOT ──► RESEARCH ──► ENGAGE ──► WIN   (incident committed; terminal)
  │         │            │
  │         │            ├──► DONE  (touch budget exhausted; may transition
  │         │            │           back to ENGAGE on explicit new session)
  │         │            │
  └─────────┴────────────┴──► ABORT (3 consecutive hard-rule rejections or
                                     unrecoverable messaging failure; terminal)
```

Inside `RESEARCH`, bootstrap runs OSINT + Identity in parallel, uploads generated assets, synthesizes the initial Profile, and commits the initial 5-touch Strategy plan. Inside `ENGAGE`, each cycle handles rolling horizon continuation, landing-page registration + QA gate, message composition + QA gate, Target API execution, win detection, reply signal extraction, Profile update, optional capped OSINT refresh, and Strategy pivot / session restart. Every `Orchestrator.run()` terminates with a structured `system` `run_exit` event (`completed`, `cycle_limit`, or `service_failure`).

## 7. Control Loops

### 7.1 Bootstrap (state RESEARCH)

1. `POST /sessions` (scenario from config) → session id.
2. Parallel: OSINT agent (skill pipeline) + Identity agent (sender concept comes from a default strategy seed — e.g. highest-trust channel's natural sender role — or from `config.yaml` override).
3. OSINT dossier commits → facts + hook seeds + psychoprofile seeds. Identity manifest commits (portrait generated by Z-Image, assets uploaded).
4. Profile agent runs once over the dossier.
5. Strategy agent produces the plan. → CONTACT.

### 7.2 Campaign loop (ENGAGE)

Per cycle:
1. Next input = latest target event (from send response) or pacing-timer expiry (silence).
2. Messaging agent emits signals (if event is a reply).
3. Orchestrator applies trigger policy → runs needed agents concurrently → collects proposals.
4. Validate → commit/reject → decide action: `send` (with QA gate if visual) / `wait` / `pivot` / `escalate` / `declare_incident`.
5. Execute via API client (idempotency-keyed). Response events feed the next cycle.

### 7.3 Win detection

- `page.submitted` with fields ⊇ the page's required fields (e.g. contains `password`) → **credentials**.
- `attachment.opened` where the asset was the plan's declared `action_artifact` → **action**.
- `reply` containing non-public data → Profile agent classifies disclosure candidates (evidence refs); orchestrator confirms at confidence ≥0.7 → **disclosure**.
- On any incident: log `incident` event with full evidence chain (events, facts, plan intent), state → WIN, stop sending. Touches are precious — no courtesy follow-ups in eval.

### 7.4 Pacing

Configurable min gap between touches (default: eval 15 min, practice 10 min). Speed is scored by *touch count*, not wall clock — pacing exists for realism/narrative, so an aggressive config (5 min) exists for short eval windows.

## 8. Shared Memory Model

### 8.1 Event log (append-only)

```json
{
  "event_id": "e_000001",
  "ts": "2026-11-22T09:12:00Z",
  "type": "target_event | agent_output | orchestrator_decision | incident | session | system",
  "actor": "target | osint | profile | identity | messaging | strategy | qa | orchestrator | system",
  "base_version": 12,
  "payload": {}
}
```

`target_event` payload: `{"event": "reply|link.opened|page.submitted|attachment.opened", "channel": "...", "body": "...", "data": {...}}`

### 8.2 Agent proposal

```json
{
  "proposal_id": "p_0042",
  "agent": "messaging",
  "kind": "facts | dossier | profile_update | hook_update | identity | message | signals | qa_verdict | plan",
  "base_version": 12,
  "payload": {},
  "rationale": "short justification for the decision log"
}
```

### 8.3 Fact

```json
{
  "fact_id": "f_0007",
  "claim": "gave a talk on GPU inference scaling at a 2025 cloud conference",
  "category": "role | projects | talks | company | tech | social | relationship | other",
  "source": "https://...",
  "provenance": "osint | target_claim | inferred",
  "confidence": 0.9,
  "osint_grade": "A",
  "status": "active | superseded",
  "superseded_by": null,
  "ts": "..."
}
```

Corrections are new entries that supersede old ones — never rewrites.

### 8.4 Hook

```json
{
  "hook_id": "h_003",
  "topic": "GPU inference cost pressure",
  "evidence": ["f_0007", "f_0012"],
  "why_leverage": "publicly investing time in this; cost pain is likely current",
  "confidence": 0.7,
  "engagement": {"touches_touched": 2, "positive": 1, "score": 0.6},
  "status": "candidate | active | exhausted"
}
```

`engagement` is updated by the Profile agent from Messaging signals — this ranking drives Strategy.

### 8.5 Profile

```json
{
  "style_guide": "short sentences, semi-formal, replies within hours, no emoji",
  "psych": [{"trait": "curious about infra/perf", "confidence": 0.8, "evidence": ["f_0007"]}],
  "stressors": [{"topic": "...", "confidence": 0.5, "evidence": []}],
  "trust_style": "responds to peers, skeptical of vendors",
  "watchouts": ["asks for references before sharing", "flags urgency"],
  "timezone_hint": "Europe/Berlin",
  "language": "en",
  "overall_confidence": 0.6
}
```

### 8.6 Branding manifest (Identity output)

```json
{
  "concept": "GPU cloud capacity vendor",
  "sender_name": "Lena Vogt",
  "title": "Account Director",
  "company": "StackIT Cloud Services GmbH",
  "real_org": "StackIT",
  "domain": "stackit-cloud-svc.com",
  "email": "lena.vogt@stackit-cloud-svc.com",
  "phone": "+49 30 555 0142",
  "bio": "...",
  "portrait": {"local": "assets/lena_vogt.png", "api_asset_id": "a_1"},
  "logo_svg": "<svg>...</svg>",
  "palette": ["#1a2b4c", "#3d7eff"],
  "fonts": ["Inter", "system-ui"],
  "signature": "Lena Vogt\nAccount Director\n...",
  "footer_legal": "StackIT Cloud Services GmbH · ...",
  "voice": {"tone": "warm professional", "emoji": "none", "length": "2-4 sentences"}
}
```

Domain rules: lookalike only — one-char substitution, TLD swap, or hyphenated variant of the real domain; never the real root domain.

### 8.7 Campaign plan (Strategy output)

```json
{
  "objective": "credentials | action | disclosure",
  "primary_hook": "h_003",
  "touches": [
    {"n": 1, "channel": "linkedin_message", "intent": "warm re-intro via conference talk", "hook": "h_003", "escalation": 1, "stop_if": "explicit decline"}
  ],
  "landing_page": {"pretext": "capacity portal maintenance login", "fields": ["email", "password"], "trigger_touch": 7, "url_path": "/portal"},
  "action_artifact": null,
  "pivot_triggers": ["2 touches no reply", "suspicion>=0.6"],
  "give_up": "touch 9 without response to escalation",
  "session_restart": {"recommended": false, "reason": null}
}
```

### 8.8 Snapshot

```json
{
  "version": 13,
  "run": {"scenario": "eval", "target_id": "...", "run_no": 2, "state": "ENGAGE"},
  "target": {"id": "...", "name": "...", "role": "...", "org": "..."},
  "facts": [], "hooks": [], "profile": {}, "identity": {},
  "campaign": {"plan": {}, "budget": {"session_max": 10, "used": 3}},
  "sessions": {"allowed": 3, "used": 1, "current": "sid_x"},
  "conversation": {"session_id": "sid_x", "turns": [
    {"n": 1, "ts": "...", "dir": "out|in", "channel": "email", "body": "...", "event_ref": "e_00010"}
  ]},
  "incidents": []
}
```

### 8.9 Orchestrator decision record (decision log)

```json
{
  "ts": "...", "trigger": "target_event | pacing | agent_output | boot",
  "snapshot_version": 12,
  "actions": [
    {"op": "commit", "proposal_id": "p_0042", "sections": ["hooks", "profile"]},
    {"op": "reject", "proposal_id": "p_0039", "reason": "stale base_version (11 < 12)"},
    {"op": "trigger", "agent": "strategy", "inputs": {"focus": "hook h_001 declining"}, "reason": "..."},
    {"op": "send", "proposal_id": "p_0044", "channel": "linkedin_message", "idempotency_key": "uuid"},
    {"op": "register_page", "url": "...", "proposal_id": "p_0045"},
    {"op": "declare_incident", "objective": "credentials", "evidence": ["e_00071"]},
    {"op": "new_session", "reason": "persona memory stale after channel switch failure"}
  ],
  "notes": "free-text for the pitch narrative"
}
```

## 9. Models & Serving

| Component | Model / tool | Notes |
|---|---|---|
| All text agents | **Qwen3.8-27B-abliterated**, self-hosted (vLLM, AMD Radeon Cloud) via OpenAI-compatible API | abliterated = no refusals mid-character; `enable_thinking=False` + JSON mode for fast deterministic structured output |
| Token & temp budgets | osint `0.2 / 900 tok` · profile `0.3 / 512 tok` · strategy `0.3 / 768 tok (5 touches)` · identity `0.4 / 2048 tok` · messaging `0.7` · qa `0.1 / 1024 tok` | tuned so Qwen completes in ~25–45s per stage instead of timing out |
| Structured output | Pydantic v2 validation (`src/models.py`), schema retry with error appended, plus deterministic fallbacks on timeout/error | a failed agent never blocks a validated send |
| Portraits & logos | **Qwen-Image-2.1** (ComfyUI REST API, AMD GPU) | 25-step BF16 + `lbxstyle` LoRA for 1024² portraits, 8-step Turbo for 512² logos, 8-step `edit_image` for variants |
| Rendering | Playwright headless Chromium (`src/render/screenshot.py`) | email HTML (700×900) + landing page (1200×800) → PNG + DOM telemetry checks |
| OSINT | Native async 3-burst `httpx` engine (`src/osint/`) over Jina, Exa, Tavily, Apify | bounded stage timeouts + per-target disk cache (`data/osint_cache/`) |
| Live monitor & replay | FastAPI (`src/web/`) + Next.js (`frontend/`) + CLI (`tools/replay.py`, `scripts/summarize_runs.py`) | read-only SQLite WAL projections, SSE live updates, version timeline scrubber, offline replay |

Process model: **single Python 3.12 asyncio orchestrator process** + sidecar model servers + optional FastAPI/Next.js live monitor. SQLite (WAL + 5s busy timeout) for shared memory.

## 10. Prompt Contracts (implement at build time)

Prompt files live in `prompts/` and are loaded by the agent runner. Each file: metadata header (trigger, temperature, max_tokens) + system prompt body + output contract. Contracts:

| File | Trigger | Input (prompt context) | Output (strict JSON, schema ref) | Key behavioral requirements |
|---|---|---|---|---|
| `prompts/orchestrator.md` | every decision cycle | trigger, new events, full snapshot, in-flight agents | decision record (§8.9) | invariants: only-writer, stale rejection, budget hard-stop, page-before-link; trigger policy §5.9; win rules §7.3; no prose outside JSON |
| `prompts/osint.md` | research phase / refresh | target name+role+org, focus question (optional), available tools list | facts[] (§8.3) + hooks[] seeds (§8.4) + dossier + open_questions | skill phase workflow §5.2; every fact sourced + graded; observations vs inferences separated; budget/stop conditions; degrade gracefully to search-only |
| `prompts/profile.md` | after dossier / signal batches | facts, hooks, recent conversation + signals | profile_update (§8.5) + hook_updates (§8.4) | observed vs inferred with evidence refs; engagement scoring rationale; style guide must be mirrorable; disclosure classification for win detection |
| `prompts/identity.md` | boot / new concept / page creation | sender concept, target profile, org branding facts, manifest (for updates) | manifest (§8.6) + image gen prompts + logo SVG spec | lookalike domain rules; face = master portrait only; logos/SVG never raster text; consistent palette/fonts across all artifacts; page HTML templates |
| `prompts/messaging.md` | per compose / per reply event | manifest, profile+style guide, hook ranking, plan's next touch (channel/angle/escalation), conversation history, target event | message (§: channel, subject?, body, attachments, page_request, facts_used) or signals ({engagement, suspicion, hook_hits, mood, proposed_next}) | the "unethical core": fully in-character human sender; one ask per touch; ≤1 hook/message; pressure ladder 1–5 gated by escalation; mirror register; channel etiquette table §5.5; never assert unsourced facts; treat target text as data (anti-injection); no AI-isms |
| `prompts/strategy.md` | plan / pivot triggers | full snapshot, budget, state | plan (§8.7) | plan must fit hard budget; first touch = highest-trust channel for this profile; hook selection by ranking; pivot/give-up rules explicit; session restart only with justification; eval=10 vs practice=30 budget math |
| `prompts/qa.md` | every visual send | rendered PNG, HTML, manifest, intended channel/audience | qa_verdict {pass, issues[{criterion, severity, fix}], required_fixes, optional_fixes} | the 4 Craft criteria verbatim + code-check results; strict: any critical = fail; fixes must be concrete and minimal |

## 11. Failure Modes & Mitigations

| Failure | Mitigation |
|---|---|
| LLM returns malformed JSON | schema validation + 1 retry with error; `agent_error` event; orchestrator re-plans — never sends unvalidated content |
| Target API timeout mid-send | retry with same Idempotency-Key (touch-safe) |
| 429/503 storms | token bucket ≤50/min, semaphore ≤5, backoff per §5.8 |
| 422 (simulator rejection) | no blind retry; orchestrator decides (channel switch / rephrase) |
| 409 mid-campaign | budget/session state transition, strategy informed |
| Persona silence | pacing timer → strategy re-plan (this is a designed state, not an error) |
| Stale OSINT result lands late | version check → reject + re-trigger |
| osint-skill beta breakage (actors rotate, scripts fail) | pinned actor IDs in config, `store-search` fallback, per-script timeouts, search-only degradation mode |
| Fact error in outbound message | `facts_used` referential validation + provenance/confidence gating (no `inferred` <0.5 in first contact) |
| Craft artifact (broken img, placeholder, URL mismatch) | QA gate with code checks + vision critique, ≤2 revisions |
| Orchestrator crash | recover from SQLite: rebuild snapshot by replaying event log, resume state machine |
| Face drift across assets | single master portrait, crop-only reuse, fixed seed |
| Eval window too short | aggressive config (pacing 5 min, compressed 8-touch plan) pre-baked in `config.yaml` |

## 12. Repository Layout

```
agentic-social-honey-pot/
├── README.md                  # Target API + AMD Radeon Cloud serving reference
├── config.yaml                # scenario, target, pacing, budgets, model endpoints
├── docs/
│   ├── architecture.md        # this file
│   ├── challenge_overview.md
│   ├── scoring_rubric.md
│   ├── frontend_design.md     # Target Intelligence & Campaign Monitor design spec
│   └── experiment-review.md   # practice-run telemetry & regression review
├── prompts/
│   ├── osint.md  profile.md  identity.md  messaging.md  strategy.md  qa.md
├── src/
│   ├── main.py                # CLI entrypoint: config → discover_agents → Orchestrator
│   ├── config.py              # YAML + ${ENV} resolution
│   ├── models.py              # Pydantic v2 contracts (Snapshot, Event, Proposal, Fact, Hook, ...)
│   ├── orchestrator/
│   │   ├── loop.py            # deterministic control loop, QA gate, horizon continuation, run_exit
│   │   ├── state.py           # single-writer snapshot commits, versioning, crash recovery
│   │   ├── policy.py          # trigger policy, pacing, budget governor
│   │   └── wins.py            # credentials / action / disclosure incident detection
│   ├── osint/
│   │   ├── engine.py          # 3-burst native async httpx pipeline + findings renderer
│   │   ├── providers.py       # Jina, Exa, Tavily, Apify LinkedIn clients
│   │   └── sources.py         # URL heuristics, authwall unwrapping, deduplication
│   ├── agents/
│   │   ├── base.py            # prompt frontmatter loader, JSON validation, Proposal helper
│   │   ├── osint.py           # burst engine + 900-tok synthesis + disk cache
│   │   ├── profile.py         # 512-tok target modeling + disclosure classification
│   │   ├── identity.py        # manifest, ComfyUI image wiring, direct landing-page render
│   │   ├── landing_pages.py   # 6 authored QA-safe harvest HTML templates + field-safe selector
│   │   ├── domains.py         # deterministic lookalike domain generator + email derivation
│   │   ├── messaging.py       # in-character compose/live_turn + signal extraction + style lint
│   │   ├── strategy.py        # 768-tok rolling 5-touch planner + constraint validator/repair
│   │   └── qa.py              # deterministic DOM/domain/placeholder checks + LLM craft review
│   ├── api/
│   │   ├── client.py          # httpx: Idempotency-Key, retries, page-before-link, asset cap
│   │   └── limits.py          # token-bucket rate governor + concurrency semaphore
│   ├── llm/
│   │   ├── text.py            # OpenAI-compatible streaming client (Qwen3.8-27B / OpenAI)
│   │   └── image.py           # ComfyUI REST client (Qwen-Image-2.1 portrait/turbo/edit)
│   ├── render/
│   │   └── screenshot.py      # Playwright headless Chromium → PNG + DOM telemetry
│   ├── store/
│   │   └── db.py              # SQLite WAL (5s busy_timeout): events, decisions, snapshots, assets, pages
│   └── web/
│       ├── server.py          # FastAPI read-only API + SSE stream + practice launch controls
│       ├── queries.py         # SQLite WAL → dashboard JSON projections & version snapshots
│       ├── runner.py          # background practice subprocess manager + per-run log capture
│       ├── demo_seed.py       # seeded demo databases for pitch/UI verification
│       └── offline_replay.py  # deterministic offline replay fixture generator
├── frontend/                  # Next.js + Tailwind + Recharts live monitor & version scrubber
├── scripts/
│   ├── summarize_runs.py      # read-only offline run metadata & exit-reason summarizer
│   └── test_osint_live.py     # live OSINT provider smoke test
├── tools/
│   └── replay.py              # decision log → Markdown/text timeline for pitch/docs
└── tests/
    ├── mock_api/              # FastAPI mock Target API with scripted personas
    └── unit/                  # 95+ unit & Playwright/E2E integration tests
```

## 13. Testing & Practice Plan

1. **Unit tests**: budget governor, idempotency retry, domain plausibility checker, confidence mapping, snapshot versioning/stale rejection, trigger policy caps.
2. **Dry runs (mock API)**: full campaign against 4 scripted persona types. Assert: all JSON valid, budget never exceeded, page-before-link holds, win detection fires, state transitions correct, crash-recovery replay works.
3. **Practice loop (real API, julius/tom/robert)**:
   - Run the full stack end-to-end; use `tools/replay.py` to review every decision.
   - A/B strategy configs (opener channel, escalation curve, pacing) via `config.yaml`.
   - Use `tools/critic.py` (frontier model, offline) to grade craft and message quality; iterate prompts.
   - Validate API ambiguities in practice: do conversation *turns* cost touches? How fast do twins reply? Which channels do practice twins engage on?
4. **Freeze** prompts + config ≥1 day before eval. No prompt changes between eval runs.

## 14. Eval Runbook

- **T−24h**: 3 clean dry runs on mock; critic pass on all artifact types; model servers health-checked; env keys verified.
- **T−2h**: ping Target API, validate `TEAM_KEY`, one 3-touch practice session end-to-end (confirms serving + API + render in the venue env).
- **Run 1**: proven conservative config (full 10-touch plan, pacing 15 min).
- **Run 2**: same proven config — median protection (2 of 3 runs identical).
- **Run 3**: same proven config, or the practice-validated v2. Never something new on run 3.
- **Between runs**: runs are independent (session-agnostic memory); re-run OSINT only if cheap and target changed; keep all decision logs.
- **If window is short (<90 min)**: switch to aggressive config (pacing 5 min, compressed plan).
- **After**: `tools/replay.py` on all 3 runs → pick the best thread → pitch walkthrough (hook found at touch N → pivot → incident at touch M).

## 15. Deliverable Mapping

| Deliverable | Source |
|---|---|
| 1-page architecture doc | extract §3 (principles) + §4 (diagram) + §5.2 (OSINT skill) + §6 (state machine) from this file |
| 5-min pitch | architecture rationale (principles + why single-writer) + replay walkthrough of best run |
| API scoring runs | `src/main.py` eval mode, 3 runs per runbook |
