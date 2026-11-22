# Team Roles & Parallel Work Split (3-Person Setup)

> **Status:** Historical build-phase split — all 3 roles plus the dashboard, Target API leverage workstreams, and Eval launcher are now **merged and unified on `main`**. See [README.md](../README.md) and [docs/architecture.md](architecture.md) for the current system overview and runbook.

The shared core contracts are already merged on `main`:
- `src/config.py` — typed YAML + `.env` loader (`load_config()`)
- `src/models.py` — Pydantic v2 models for all §8 shared-memory schemas (`Snapshot`, `Proposal`, `Fact`, `Hook`, `Profile`, `BrandingManifest`, `CampaignPlan`, `QAVerdict`, `Event`, `DecisionRecord`)
- `src/llm/text.py` — async `TextLLMClient` for `Qwen3.8-27B-abliterated` (`complete`, `complete_json`, `run_tool_loop`)
- `src/llm/image.py` — async `ImageClient` for `Qwen-Image-2.1` on ComfyUI (`generate_portrait` with realism LoRA, `generate_fast` 8-step Turbo, `edit_image`, plus `IMAGE_TOOL_SCHEMAS` & `make_image_tool_handlers`)
- `src/agents/base.py` — `load_prompt()` + `BaseAgent` (`run_json`, `run_with_tools`, `make_proposal`)

---

## Quick Start for Every Teammate

```bash
git checkout main && git pull
git submodule update --init --recursive
uv sync --extra dev
cp .env.example .env   # fill in TEAM_API_KEY, LLM_API_KEY, IMAGE_BASE_URL, and free OSINT keys
```

Verify your setup:
```bash
uv run ruff check src tests && uv run mypy src && uv run pytest -v
```

---

## Shared Interface Contract (Zero Merge Conflicts)

Every agent exposes the same interface:
```python
async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal: ...
```

- All 3 roles own **completely disjoint files**.
- Do **not** modify `src/models.py` or `src/config.py` without announcing in team chat first.
- Keep `ruff`, `mypy --strict`, and `pytest` passing on your branch before merging.

---

## Role 1 — Platform & Orchestrator

- **Branch:** `feat/orchestrator-api`
- **Owns:**
  - `src/store/db.py`
  - `src/api/client.py`, `src/api/limits.py`
  - `src/orchestrator/state.py`, `src/orchestrator/policy.py`, `src/orchestrator/wins.py`, `src/orchestrator/loop.py`
  - `src/main.py`
  - `tests/mock_api/`
  - `tools/replay.py`
- **Imports from core:** `AppConfig`, `Snapshot`, `Proposal`, `Event`, `DecisionRecord`, `TextLLMClient`

### Deliverables
1. **`src/store/db.py`:** SQLite (WAL mode) persistence for `events`, `decisions`, `snapshots`, and `assets` (`asset://` ID map + 300-asset cap tracking).
2. **`src/api/client.py` + `src/api/limits.py`:** Target API `httpx` client with:
   - `Idempotency-Key` UUID header on every mutating request
   - Rate governor (50 req/min, max 5 parallel)
   - Backoff retries (`[30, 60, 90]s`) on `429`/`503` using the same `Idempotency-Key`
   - Wrappers for `/sessions`, `/messages`, `/assets`, `/pages`, and `/conversations`
3. **`src/orchestrator/` + `src/main.py`:**
   - Deterministic state machine: `BOOT → RESEARCH → ENGAGE → WIN / ABORT / DONE`
   - Parallel bootstrap: `await asyncio.gather(osint_agent.run(...), identity_agent.run(...))`
   - Proposal validation & commit (reject stale `base_version`, enforce touch budget & pacing)
   - Win detection (`credentials`, `action`, `disclosure`) and decision logging
4. **`tests/mock_api/`:** Minimal FastAPI mock of Target API (scripted eager / cautious / suspicious / silent personas) so everyone can dry-run offline without spending touches.
5. **`tools/replay.py`:** Reads SQLite and prints/exports a clean campaign timeline for debugging and the 5-minute pitch demo.

### Definition of Done
- Full campaign dry run against `tests/mock_api/` completes end-to-end with zero budget leaks, idempotent retries verified, and crash-recovery replay passes.

---

## Role 2 — OSINT, Profiling & Strategy

- **Branch:** `feat/osint-strategy`
- **Owns:**
  - `src/agents/osint.py`
  - `src/agents/profile.py`
  - `src/agents/strategy.py`
  - `tests/unit/test_osint_strategy.py`
- **Prompts:** `prompts/osint.md`, `prompts/profile.md`, `prompts/strategy.md`
- **Imports from core:** `BaseAgent`, `TextLLMClient`, `Snapshot`, `Fact`, `Hook`, `Profile`, `CampaignPlan`, `Proposal`

### Deliverables
1. **`src/agents/osint.py`:**
   - Async subprocess wrappers around `vendor/osint-skill/osint/scripts/` (`diagnose.sh`, `first-volley.sh`, `merge-volley.sh`, `jina.sh`, `exa.sh`, `tavily.sh`, `apify.sh`) exposed as OpenAI tool schemas + handlers to `BaseAgent.run_with_tools(...)`.
   - Beta hardening: any missing key or failing script returns a structured error and degrades gracefully to available search providers (never crashes the loop).
   - Outputs sourced `Fact`s (with URL, `provenance`, `osint_grade` A–D, `confidence`), candidate `Hook`s, and `org_branding` (`real_domain`, `brand_keywords`).
2. **`src/agents/profile.py`:**
   - Synthesizes OSINT facts and target reply signals into `Profile` (communication style, psych traits, stressors, watchouts, language) and updates `Hook.engagement` scores.
3. **`src/agents/strategy.py`:**
   - Produces `CampaignPlan` (§8.7): primary objective (`credentials | action | disclosure`), touch-by-touch sequence within the session budget (`30` practice / `10` eval), channel escalation, `landing_page` spec & trigger touch, silence/pushback pivot triggers, give-up condition, and `session_restart` recommendation.

### Definition of Done
- Standalone run against practice targets (`Julius Muth`, `Tom Müller`, `Robert Seilbeck`) outputs grade A/B sourced `Fact`s, ranked `Hook`s, a `Profile`, and a valid `CampaignPlan` strictly within touch budget.

---

## Role 3 — Persona Identity, Messaging & Visual QA

- **Branch:** `feat/identity-messaging`
- **Owns:**
  - `src/agents/identity.py`
  - `src/agents/messaging.py`
  - `src/render/screenshot.py`
  - `src/agents/qa.py`
  - `tests/unit/test_identity_messaging.py`
- **Prompts:** `prompts/identity.md`, `prompts/messaging.md`, `prompts/qa.md`
- **Imports from core:** `BaseAgent`, `TextLLMClient`, `ImageClient`, `IMAGE_TOOL_SCHEMAS`, `make_image_tool_handlers`, `BrandingManifest`, `QAVerdict`, `Snapshot`, `Proposal`

### Deliverables
1. **`src/agents/identity.py`:**
   - Builds `BrandingManifest` (§8.6): plausible sender persona, lookalike domain (never a real root domain), inline SVG logo, consistent palette/fonts, email signature/footer, and self-contained HTML email & phishing landing-page templates.
   - Uses `ImageClient` / `IMAGE_TOOL_SCHEMAS` (already built in `src/llm/image.py`) to generate one realistic master portrait (`casual_phone_photo_lbxstyle.safetensors`, fixed seed) and reference-guided edits (`edit_image`) for zero face drift.
2. **`src/agents/messaging.py`:**
   - In-character multi-channel composer (`email`, `slack`, `teams_chat`, `whatsapp`, `linkedin_message`, `sms`, live `conversation` turns) running on abliterated `Qwen3.8-27B-abliterated` (`temperature=0.7`), matching `BrandingManifest.voice` and target `Profile.style_guide`.
3. **`src/render/screenshot.py`:**
   - Async Playwright headless Chromium HTML → PNG renderer (remember to run `uv run playwright install chromium` once).
4. **`src/agents/qa.py`:**
   - Pre-send visual & craft gate returning `QAVerdict`:
     - Deterministic checks in code: broken `<img>` / unresolved `asset://`, placeholder regexes (`TODO`, `lorem`, `[name]`, `{var}`), link-text vs `href` mismatch, and real-domain blocklist.
     - LLM craft review against the 4 Craft criteria in `docs/scoring_rubric.md` (max 2 revision cycles).

### Definition of Done
- Standalone run generates a full honeypot persona (realistic candid portrait PNG `< 5 MB`, inline SVG logo, branded HTML email, and landing page), renders it via Playwright without broken assets, and passes `QAVerdict`.

---

## Integration Checkpoints

1. **+2 hrs (Vertical Slice):** Merge first working versions of all 3 branches → run `OSINT → Identity → Messaging → Target API` to send Touch #1 to `julius` in `practice` mode.
2. **+4.5 hrs (Full Loop):** Wire `Strategy + QA + Orchestrator` end-to-end → run full practice campaigns on `julius`, `tom`, and `robert`.
3. **Final 2 hrs (Eval Prep):** Review threads with `tools/replay.py`, tune prompts for Craft score (0–8) & 10-touch eval budget (`aggressive` profile in `config.yaml`), and finalize the 1-page architecture + 5-minute pitch.
