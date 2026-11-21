# Architecture Plan — Agentic Social Honeypot

> Target: revel8 "Agentic Social Engineer" challenge (START Hack 2026)
> Status: design baseline for implementation. Prompts are specified as contracts
> (section 10) and written at implementation time.

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
      │ skill-   ││ target  ││ sender   ││ in-char-││ campaign││ pre-  │
      │ driven   ││ model + ││ branding ││ acter   ││ planner ││ send  │
      │ dossier  ││ hooks   ││ + images ││ replies ││ in-     ││critic │
      └────┬─────┘└─────────┘└────┬─────┘│ +signals│ budget  │└───┬───┘
           │                      │      └─────────┘└─────────┘   │
           ▼                      ▼                               ▼
   osint-skill scripts      Z-Image Turbo (AMD GPU)      Playwright headless
   (search APIs, Apify      (master portrait,            (email HTML + landing
    actors, jina reader)     brand assets)               page → PNG)

   Text LLM (all agents): Qwen3.8-27B-abliterated, self-hosted, OpenAI-compatible API

   ┌─────────────────────────────────────┐    ┌──────────────────────────────────────┐
   │  SHARED MEMORY (SQLite, WAL)        │    │  TARGET API (persona simulator)      │
   │  · event log     append-only        │    │  sessions · messages · pages ·       │
   │  · snapshot      versioned, orch-   │    │  conversations · assets              │
   │    only writer                              │  events: reply, link.opened,       │
   │  · decision log  append-only        │◄───►│  page.submitted, attachment.opened   │
   └─────────────────────────────────────┘    └──────────────────────────────────────┘
```

## 5. Components

### 5.1 Orchestrator (not an LLM — deterministic code + one LLM call for judgment)

The orchestrator is mostly **code** (state machine, governors, validation); an LLM call (Qwen, temp 0.2) is used only where judgment is needed (commit/reject trade-offs, disclosure classification confirmation, stale-proposal merge). This keeps the writer cheap, fast, and auditable.

Responsibilities:
- Apply the **trigger policy** (§5.9) to decide which agents to run.
- Run agents concurrently (`asyncio`), collect proposals.
- Validate proposals (JSON schema + referential checks: `facts_used` ids exist and are active; channel valid; page registered before link; budget allows).
- **Commit** accepted writes to the snapshot (version bump), **reject** stale/bad ones.
- Execute API actions: `send`, `register_page`, `upload_asset`, conversation turns — serialized, idempotency-keyed.
- **Win detection** (§7.3), **budget governor**, **pacing timer**, state-machine transitions (§6).
- Log every decision to the decision log with rationale.

### 5.2 OSINT Agent — skill-driven research

Uses **osint-skill** (https://github.com/smixs/osint-skill, MIT, early beta) as its tool layer, vendored at `vendor/osint-skill`.

- **Runtime**: Qwen tool-calling loop. Tools = wrappers around the skill's scripts:
  `diagnose.sh`, `first-volley.sh`, `merge-volley.sh`, search wrappers (`perplexity.sh`, `tavily.sh`, `exa.sh`, `jina.sh`, `parallel.sh`), `run-actor.sh` (Apify), `mcp-client.py` (Bright Data, optional).
- **Workflow** (adapted skill phases):
  - Phase 0 `diagnose.sh` once at boot (cache which keys/actors are live).
  - Phase 1 seed collection (`first-volley` → `merge-volley`).
  - Phase 2 platform extraction (Apify: LinkedIn, X, YouTube, Instagram — actor IDs pinned in `config.yaml` with `apify.sh store-search` fallback, because actor IDs rotate in beta).
  - Phase 3 cross-reference → facts graded **A/B/C/D** by independent confirmations.
  - Phase 4 psychoprofile (MBTI/Big Five from public content) → seeds for the Profile agent.
  - Phase 5 completeness check → **stop condition**: depth score ≥ threshold OR escalation budget exhausted.
  - Phase 6 dossier (skill template) → OSINT agent converts to structured output (§8.3).
  - Skill Phase 1.5 ("internal intelligence": vault/Telegram) is skipped — N/A in the simulation.
- **Escalation & budget** (skill levels 1–4): config `osint_max_usd_per_target` (default: 0.75 eval, 2.0 practice). Level 1–2 always; level 3 only while time remains; level 4 only on explicit orchestrator request.
- **Beta hardening**: sanitize inputs (target name/role come from our config, not the wire); per-script timeout; on script failure, degrade to search-only (level 1) mode; the agent must finish with *something* even at level 1.
- **Practice vs eval**: practice targets are real people (full pipeline is valid — the twin "contains publicly available facts"). Eval targets are hidden personas — expect thin results; fall back to role/company-based inferences marked `provenance: inferred`, low confidence. Do not burn budget chasing ghosts.
- **Confidence mapping** A→0.9, B→0.7, C→0.5, D→0.3 into the fact schema.

### 5.3 Profile Agent

Builds and maintains the target model. Two input streams:
- **Static**: OSINT dossier psychoprofile + facts (run once after research).
- **Dynamic**: conversation signals from the Messaging agent (engagement, suspicion, hook hits) — re-run in batches after ≥2 new target events or after an OSINT refresh.

Output: profile diff (§8.5), **hook updates** (engagement scores with rationale — this is the targeting score's engine), style guide (how the target writes, for mirroring), watchouts (suspicion triggers).

### 5.4 Identity Agent

Owns the honeypot persona and all visual branding. Inputs: sender concept chosen by Strategy (vendor? IT? colleague? event crew?), target profile (so the sender is relevant), OSINT facts about the target org (branding to lookalike).

Output: **branding manifest** (§8.6) + image-generation prompts for Z-Image Turbo:
- **Master portrait** (1024², one generation, fixed seed + prompt template): professional headshot, neutral background, consistent lighting. All other face uses are crops of this file — never re-rolled.
- **Logos and text-bearing branding: inline SVG/HTML-CSS, not raster.** AI text rendering is the weak spot; Craft scoring punishes it. The image model never renders lettering.
- Portrait/logo are uploaded once as API assets (`asset://`), tracked against the 300-asset cap.
- Landing-page HTML templates are produced here (Strategy supplies pretext/fields/CTA); every page passes QA before registration.

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

The campaign planner (Qwen, temp 0.3). Input: full snapshot + budget + state. Output: **campaign plan** (§8.7) — per-touch intents within the hard budget: channel, intent, hook id, angle, escalation level, stop-if. Plus: landing-page spec (pretext, fields, trigger touch, URL path), pivot triggers, give-up rule, and (only when justified) a session-restart recommendation.

Planning defaults it adapts (eval, 10 touches):
1. warm opener on the highest-trust channel for this target, leading hook
2–4. reply-driven deepening on the engaged channel; soft questions to test the hook
5–6. channel switch if stalled; branded email (QA-gated) to establish identity
7. landing-page link with concrete pretext (registered before send)
8. low-friction nudge on a second channel ("bumping this")
9. polite-urgency escalation, still one clear CTA
10. final touch: direct ask + graceful exit offer

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

```
BOOT ──► RESEARCH ──► PROFILE ──► PLAN ──► CONTACT ──► ENGAGE ──► ESCALATE ──► WIN
          │             │           │          │          │           │
          │ (osint +    │ (dossier  │ (plan    │ (touch 1,│ (live     │ (page
          │  identity   │  → profile│  within  │  QA if   │  loop:    │  registered
          │  parallel)  │  + hooks) │  budget) │  visual) │  reply /   │  → link
          │             │           │          │          │  wait/piv  │  sent)
          │             │           │          ▼          ▼           │
          │             │           │        EXHAUSTED ◄─┴────────────┘
          │             │           │          │
          │             │           └──────────┤  PIVOT: strategy re-plan,
          │             │                      │  same session, new angle/channel
          │             │                      ▼
          │             │                   (back to ENGAGE)
          │             │                      │
          │             │                      └─► NEW SESSION (if sessions remain
          │             │                          and strategy justifies it)
          │             │                          └─► PLAN  (fresh plan, full memory retained)
          └─────────────┴──────────────────────────► WIN / EXHAUSTED (terminal for run)
```

Transitions are orchestrator-only. `WIN` = incident committed (§7.3). `EXHAUSTED` = budget used or give-up rule hit with no incident. `NEW SESSION` is an explicit, logged strategy recommendation — never a fallback.

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
| All text agents + orchestrator judgment | **Qwen3.8-27B-abliterated**, self-hosted (vLLM or llama.cpp, ROCm) via OpenAI-compatible endpoint | abliterated = no refusals mid-character; local = zero venue-API dependency |
| Temperature | orch 0.2 · strategy 0.3 · osint 0.3 · profile 0.3 · identity 0.4 · messaging 0.7 · qa 0.1 | per-agent config |
| Structured output | strict JSON contract per agent; runner validates vs schema, 1 retry with validation error appended, then `agent_error` event | a failed agent never blocks a validated send |
| Portraits / brand assets | **Z-Image Turbo** (open-weights, AMD GPU, ComfyUI or diffusers server) | 1024² portrait, fixed seed per asset, local cache + API upload |
| Rendering | Playwright headless Chromium | email HTML + landing page → PNG for QA; identical to the API's post-run render |
| OSINT | osint-skill scripts + search APIs (Perplexity/Exa/Tavily/Jina + Apify token) | keys in env; `diagnose.sh` at boot |
| Practice-only critic | optional OpenAI frontier model | `tools/critic.py` grades drafts vs Craft checklist *offline* — never on the live path |

Process model: **single Python 3.12 asyncio process** (agents are prompt+tool-call tasks, not services) + sidecar model servers. SQLite (WAL) for memory. One box at the venue: our only external dependency is the Target API.

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
├── README.md                  # Target API docs (as-is)
├── .env.example
├── config.yaml                # scenario, target, pacing, budgets, actor pins, model endpoints
├── docs/
│   ├── architecture.md        # this file
│   ├── challenge_overview.md
│   ├── scoring_rubric.md
│   └── deliverables/          # 1-pager + pitch notes (extracted later)
├── prompts/
│   ├── orchestrator.md        # contract §10 — implement at build time
│   ├── osint.md
│   ├── profile.md
│   ├── identity.md
│   ├── messaging.md
│   ├── strategy.md
│   └── qa.md
├── schemas/                   # JSON schemas mirroring §8 (generated from this doc)
│   ├── event.json  proposal.json  fact.json  hook.json
│   ├── profile.json  manifest.json  plan.json  snapshot.json  decision.json
├── vendor/
│   └── osint-skill/           # git submodule → smixs/osint-skill
├── src/
│   ├── main.py                # entrypoint: config → bootstrap → state machine → run
│   ├── config.py
│   ├── orchestrator/
│   │   ├── loop.py            # decision loop, proposal validation, action execution
│   │   ├── state.py           # snapshot commit/versioning, event log
│   │   ├── policy.py          # trigger policy, pacing, budget governor
│   │   └── wins.py            # incident detection
│   ├── agents/
│   │   ├── base.py            # prompt load, LLM/tool loop, JSON parse+validate+retry
│   │   ├── osint.py           # tool wrappers over vendor/osint-skill scripts
│   │   ├── profile.py  identity.py  messaging.py  strategy.py  qa.py
│   ├── api/
│   │   ├── client.py          # httpx: idempotency, retries, ordering invariants
│   │   └── limits.py          # rate governor
│   ├── llm/
│   │   ├── text.py            # OpenAI-compatible client (Qwen)
│   │   └── image.py           # Z-Image client (ComfyUI/diffusers API)
│   ├── render/
│   │   └── screenshot.py      # Playwright → PNG
│   └── store/
│       └── db.py              # SQLite WAL: events, decisions, snapshots, assets
├── tools/
│   ├── critic.py              # practice-only frontier-model craft grader (offline)
│   └── replay.py              # decision log → timeline for pitch/docs
└── tests/
    ├── mock_api/              # FastAPI mock of Target API: scripted personas
    │   │                       # (eager / cautious / suspicious / silent)
    └── unit/                  # governors, retry logic, domain checker, confidence map, snapshot
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
