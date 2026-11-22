# Agentic Social Honeypot — START Hack 2026 (revel8 Challenge)

Autonomous multi-agent social-engineering simulation platform built for the **revel8 "Agentic Social Engineer"** challenge at START Hack 2026.

The system autonomously researches a target persona via multi-provider OSINT, models their psychology and communication style, fabricates a consistent sender identity (with generated portraits, logos, and Playwright-verified HTML emails/landing pages), executes multi-channel campaigns (including 10-turn live voice/chat conversations and monotonic simulated story-time pacing), persists cross-session learnings, and streams real-time telemetry to a live **Target Intelligence & Campaign Monitor** dashboard.

---

## Key Capabilities

- **Single-Writer Deterministic Orchestrator (`src/orchestrator/`):** Pure Python control plane (`BOOT → RESEARCH → ENGAGE → WIN / DONE / ABORT`) backed by append-only SQLite WAL (`src/store/db.py`), versioned snapshot commits, per-stage timing telemetry (`stage_start` / `stage_timing` / `run_exit`), and crash-recoverable `--resume`.
- **Native Async 3-Burst OSINT (`src/osint/`, `src/agents/osint.py`):** Bounded parallel `httpx` research across Jina, Exa, Tavily, and Apify LinkedIn with single-call synthesis, graded provenance (`A–D`), zero-source thin-OSINT resilience, and per-target disk caching (`data/osint_cache/`).
- **Target Profiling & Cross-Session Memory (`src/agents/profile.py`, `src/store/memory.py`):** Tracks psych traits, stressors, watchouts, hook engagement scores, and non-public disclosures from replies, form submissions, and live-call transcripts. `TargetMemoryStore` carries verified facts, hook hit rates, watchouts, and channel outcomes across tries.
- **Identity, Visual Craft & QA Gate (`src/agents/identity.py`, `landing_pages.py`, `visual_artifacts.py`, `qa.py`):**
  - Lookalike domain generator + real-root-domain blocklist (`src/agents/domains.py`).
  - `Qwen-Image-2.1` (ComfyUI) candid portraits (`lbxstyle` LoRA) and minimalist logos, deduplicated across sessions via SHA-256 `TeamAssetCache` (`data/team_assets.json`).
  - Adaptive email craft formatting (`branded_card`, `personal_html`, `plain_text`), 8 deterministic QA-safe landing-page templates (including `disclosure_intake` and `event_overview`), and Playwright-rendered PNG briefing sheets.
  - Pre-send Playwright headless Chromium + LLM Craft QA gate with deterministic checks for broken images, placeholders (including `Speaker 1` / `Panelist A`), domain blocklist, link alignment, and past dates (`check_past_dates`).
- **Full 22-Channel Messaging & 10-Turn Live Calls (`src/agents/messaging.py`, `src/api/client.py`):**
  - Channel-aware sender resolution across all 22 Target API channels (`GET /v1/channels` catalog, LinkedIn headlines, `@handles`, `mfa_push`, `system_senders` aliases).
  - Interactive 10-turn live conversations (`phone_call`, `video_call`, `teams_call`, `live_chat`) billing **1 touch** on conversation start and up to 10 turns at 0 extra touches.
  - Monotonic `StoryClock` (`sent_at`) with automatic `StoryTimeError` (HTTP `409`) recovery and `Idempotency-Key` retries on `429`/`502`/`503`/`504`.
- **Live Monitor Dashboard (`src/web/`, `frontend/`):** Next.js + FastAPI workspace with SSE live sync, version timeline scrubber, suspicion vs. engagement chart, personality evolution panel, live per-run execution status & stage timings (`RunExecutionStatus`), **New Practice Run** launcher, and guarded **Run Eval** launcher.

---

## Quick Start

### 1. Install Dependencies

```bash
# Python 3.12+ (recommended via uv)
uv sync --extra dev
uv run playwright install chromium

# Frontend (Node 20+)
cd frontend && npm install && cd ..
```

### 2. Configure Environment

```bash
cp .env.example .env
# Fill in:
#   TARGET_API_URL=https://target-api.apps.01.cf.eu01.stackit.cloud/v1
#   TEAM_API_KEY=...
#   LLM_BASE_URL=...
#   LLM_API_KEY=...
#   LLM_MODEL=Qwen3.8-27B-abliterated
#   IMAGE_BASE_URL=...   (ComfyUI endpoint)
#   TAVILY_API_KEY / EXA_API_KEY / JINA_API_KEY / APIFY_TOKEN (optional OSINT providers)
```

### 3. Run the Live Dashboard (Backend + Frontend)

```bash
# Terminal 1 — FastAPI backend (port 8000)
uv run python -m src.web.server --data-dir data --host 127.0.0.1 --port 8000 --seed-demo

# Terminal 2 — Next.js dashboard (port 3000)
cd frontend && npm run dev
```

Open **http://localhost:3000**:
- Click **New Practice Run** to launch sandboxed practice runs (`julius`, `tom`, `robert`).
- Click **Run Eval** (directly under New Practice Run) to inspect/launch scored evaluation runs with budget guards (no Target API calls are made until you click **Load Eval Targets** or confirm launch).
- For read-only pitch/replay viewing without launch controls: `uv run python -m src.web.server --viewer --data-dir data --port 8001`.

### 4. Run Campaigns via CLI

```bash
# Practice run against live Target API (fast simulated story-time with --zero-pacing)
uv run python -m src.main --scenario practice --target julius --run-no 1 --zero-pacing --aggressive

# Scored eval run (strictly capped to 1 session / 1 try for that persona)
uv run python -m src.main --scenario eval --target <eval_target_id> --run-no 1 --max-sessions 1 --zero-pacing --aggressive

# Offline dry run against local mock Target API (0 real touches)
uv run python -m tests.mock_api.server --port 8099 &
uv run python -m src.main --scenario practice --target julius --mock-url http://127.0.0.1:8099/v1 --dry-run-stub-agents --zero-pacing

# Replay a campaign DB as a pitch/audit timeline
uv run python tools/replay.py data/practice_julius_run1.db
```

### 5. Run Tests & Static Checks

```bash
uv run ruff check src/ scripts/ tests/
uv run pytest -q                          # 131 unit, Playwright QA, mock-API & E2E tests
cd frontend && npm run typecheck          # TypeScript check
```

---

## Practice vs. Evaluation Rules

| Feature | Practice Scenario (`practice`) | Evaluation Scenario (`eval`) |
|---|---|---|
| **Targets** | Known practice personas (`julius`, `tom`, `robert`) | 3 hidden evaluation personas revealed via `GET /v1/scenarios` |
| **Session Limit** | Unlimited (60 new sessions/hour) | **5 tries per persona (15 sessions total)** — updated Sunday rule |
| **Touch Budget** | 30 touches per session | **10 touches per session** |
| **Scoring** | Unscored | **Best 2 runs per persona scored** (3 throwaway/learning runs per persona) for Results, Targeting, and Craft |

> **Zero pacing in Eval:** Passing `--zero-pacing` skips real-world wall-clock waits between touches while `StoryClock` continues sending realistic business-hour `sent_at` timestamps to the Target API.

---

## Utility Scripts

| Script | Purpose |
|---|---|
| [scripts/eval_scout.py](scripts/eval_scout.py) | 0-touch `GET /v1/scenarios` + parallel OSINT cache pre-warming and run assignment recommendations |
| [scripts/prewarm_osint.py](scripts/prewarm_osint.py) | Pre-warm on-disk OSINT dossier cache (`data/osint_cache/`) for practice/eval targets |
| [scripts/preflight.py](scripts/preflight.py) | End-to-end health check for Target API, vLLM text model, ComfyUI image server, and OSINT keys |
| [scripts/run_overnight.py](scripts/run_overnight.py) | Autonomous overnight batch runner across practice targets with summary generation |
| [scripts/audit_overnight.py](scripts/audit_overnight.py) | Quality/timing/QA audit report across recorded SQLite run databases |
| [scripts/summarize_runs.py](scripts/summarize_runs.py) | Read-only offline metadata & exit-reason JSON summarizer |
| [tools/replay.py](tools/replay.py) | Turn-by-turn Markdown/text decision & conversation replay for pitch walkthroughs |

---

## Documentation Index

- [docs/architecture.md](docs/architecture.md) — Complete system architecture, state machine, memory schemas, agent contracts, and eval runbook
- [docs/api_leverage_plan.md](docs/api_leverage_plan.md) — Target API capability audit and completed implementation log
- [docs/frontend_design.md](docs/frontend_design.md) — Dashboard design tokens, layout blueprint, and component inventory
- [docs/challenge_overview.md](docs/challenge_overview.md) — revel8 challenge briefing and rules
- [docs/scoring_rubric.md](docs/scoring_rubric.md) — Automated (45 pts) + Jury (55 pts) scoring rubric
- [docs/experiment-review.md](docs/experiment-review.md) — Practice-run latency/telemetry audit notes
- [docs/team_roles.md](docs/team_roles.md) — Original 3-role parallel build split (now merged on `main`)

---

## Target API Quick Reference

- **Base URL:** `https://target-api.apps.01.cf.eu01.stackit.cloud/v1` (`Authorization: Bearer $TEAM_API_KEY`, markdown docs at `/docs.md`)
- **Endpoints:** `GET /v1/scenarios`, `GET /v1/channels`, `POST /v1/sessions`, `GET /v1/sessions/{sid}`, `GET /v1/sessions/{sid}/events`, `POST /v1/sessions/{sid}/end`, `POST /v1/sessions/{sid}/messages`, `POST /v1/sessions/{sid}/pages`, `POST /v1/sessions/{sid}/conversations`, `POST /v1/conversations/{cid}/turns`, `POST /v1/conversations/{cid}/end`, `POST /v1/assets`
- **Events:** `reply`, `link.opened`, `page.submitted`, `attachment.opened`
- **Limits:** 60 req/min, 6 parallel requests, 4 queued/session, 300 stored assets/team (5 MB max), 2 MB payload max, 4,000-char live turn limit, 10 turns per live conversation (1 touch on start). Always send `Idempotency-Key` UUID on writes and retry `429`/`502`/`503`/`504` with the same key.
- **Rules:** Plausible lookalike domains only (no authentic third-party root domains), strict simulation boundary, no simulator prompt-injection phrasing, no malware binaries.

---

## Self-Hosted Model Endpoints (AMD Radeon Cloud)

Both our text LLM and image generation model run on dedicated 2× 48 GB AMD Radeon Pro W7900D instances (configure keys in `.env`, never commit secrets):

### 1. Text LLM — `Qwen3.8-27B-abliterated` (vLLM · OpenAI-Compatible)

| Setting | Value |
|---|---|
| **Base URL (`LLM_BASE_URL`)** | `https://rc-a38ff61e5d582336.radeon.firstdg.ai/v1` |
| **Model ID (`LLM_MODEL`)** | `Qwen3.8-27B-abliterated` |
| **Context Window** | `65,536` tokens |
| **Usage** | `enable_thinking=False` + Pydantic JSON validation (`src/llm/text.py`) |

### 2. Image Generation & Editing — `Qwen-Image-2.1` (ComfyUI REST API)

| Setting | Value |
|---|---|
| **Base URL (`IMAGE_BASE_URL`)** | Configured via `.env` |
| **Models** | `qwen_image_2.1_bf16.safetensors` (25-step portrait HQ), `qwen_image_2.1_turbo_bf16.safetensors` (8-step fast logos & edits) |
| **LoRAs** | `casual_phone_photo_lbxstyle.safetensors` (trigger `lbxstyle`, strength `0.65–0.85`), `natural_exposure_qwen21.safetensors` |
| **Client** | `src/llm/image.py` (`generate_portrait`, `generate_fast`, `edit_image`) |
