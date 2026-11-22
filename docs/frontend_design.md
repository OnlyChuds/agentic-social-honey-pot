# Frontend Design System & UI Specification

> **Project:** Agentic Social Honeypot — Target Intelligence & Campaign Monitor
> **Status:** Implemented and merged on `main` (`frontend/` + `src/web/`)
> **Stack:** Next.js 15 (App Router, TypeScript) · Tailwind CSS v4 · `shadcn/ui` · Recharts · Lucide Icons

---

## 1. Design Philosophy

This dashboard is a **live intelligence and evaluation workbench** used both during practice runs and during the 5-minute jury pitch. Every visual decision optimizes for:

1. **Zero-ambiguity status at a glance:** Judges must immediately read a target's **stage**, **suspicion risk**, **touch budget**, and **personality shift** in under 3 seconds.
2. **Evidence-backed UI:** Every trait, watchout, and hook score links back to concrete `fact_id`s, `osint_grade`s (`A–D`), and `event_id`s from SQLite WAL.
3. **Time-travel clarity:** Personality and suspicion are trajectories, not static snapshots. A global **Version Timeline Scrubber** lets the viewer replay how the target's modeled psyche and suspicion evolved from `v1` (OSINT seed) through every touch to `WIN` / `DONE`.
4. **Restrained, clinical aesthetic:** Clean dark-mode zinc/slate surfaces with razor-sharp `1px` borders, tabular monospace metrics, and high-contrast semantic accents reserved strictly for state and risk — never decorative gradients or visual clutter.

---

## 2. Design Tokens (Tailwind + CSS Variables)

All components consume semantic CSS variables (compatible 1:1 with `shadcn/ui` and `@spartan-ng` Helm tokens). Default theme is **Dark Mode** (`class="dark"` on `<html>`).

### 2.1 Core Surface & Ink Tokens

| Token | HSL / Hex | Usage |
|---|---|---|
| `--background` | `#09090B` (`zinc-950`) | App shell canvas |
| `--card` | `#111114` | Primary panels, cards, chart containers |
| `--card-elevated` | `#18181B` (`zinc-900`) | Selected sidebar row, nested callouts, popovers |
| `--muted` | `#27272A` (`zinc-800`) | Progress tracks, inactive pills, subtle dividers |
| `--border` | `#27272A` (`zinc-800`) | Hairline `1px` structural borders |
| `--border-strong` | `#3F3F46` (`zinc-700`) | Active card border, focus rings, scrubber track |
| `--foreground` | `#FAFAFA` (`zinc-50`) | Primary headings, target names, KPI numerals |
| `--muted-foreground` | `#A1A1AA` (`zinc-400`) | Roles, metadata, axis labels, timestamps |
| `--subtle-foreground` | `#71717A` (`zinc-500`) | Secondary captions, IDs (`f_0007`, `v12`) |

### 2.2 Semantic Color Discipline (Monochrome + 2 Accents Only)

To keep the workspace calm and scannable, the UI uses a **strictly monochrome dark zinc palette** for structure, engagement, stages, and controls, reserving color exclusively for **two semantic signals**:

| Signal | Token | Usage |
|---|---|---|
| **Suspicion / Risk / Abort** | `rose-400` / `rose-500` (`#F43F5E`) | Suspicion line & high-suspicion score (`>= 0.60`), downward trait revisions, `ABORT` stage |
| **Win / Positive Shift** | `emerald-400` (`#34D399`) | `WIN` stage, positive trait deltas (`+25%`, `new`), live WAL dot, incident events |
| **Everything Else** | `zinc-100` / `zinc-400` / `zinc-500` | Engagement line (`zinc-300`), neutral stages (`BOOT / RESEARCH / ENGAGE / DONE`), sidebar rows, sliders, tabs, evidence text |

No decorative icons inside section headers, no rainbow badge stacks, and no nested cards inside list rows.

---

## 3. Typography & Spacing

### Fonts (`next/font/google`)
- **UI Sans:** `Inter` (`--font-sans`) — headings, labels, conversation prose, rationale notes.
- **Data Mono:** `JetBrains Mono` (`--font-mono`) — all numeric metrics (`0.78`, `3/10`, `v14`), timestamps, `fact_id` / `hook_id` / `event_id` chips, domains, and URLs. Always apply `tabular-nums` on dynamic numbers so live updates never jitter horizontally.

### Type Scale
| Role | Classes |
|---|---|
| Target Title (H1) | `text-xl font-semibold tracking-tight text-zinc-50` |
| Panel Heading (H2) | `text-sm font-semibold tracking-tight text-zinc-100` |
| Section Eyebrow | `text-[11px] font-medium uppercase tracking-wider text-zinc-400` |
| KPI Figure | `font-mono text-2xl font-semibold tabular-nums text-zinc-50` |
| Body / Message Prose | `text-sm leading-relaxed text-zinc-200` |
| Metadata / Badges | `font-mono text-xs tabular-nums` |

### Spacing & Radius
- **Outer Shell Padding:** `p-4` (`16px`) with `gap-4` between grid cards.
- **Card Padding:** `p-4` header / `p-4` body, `rounded-xl` (`12px`), `border border-zinc-800 bg-[#111114]`.
- **Sidebar Width:** Fixed `w-80` (`320px`) on `lg+` viewports; collapsible drawer on smaller screens.

---

## 4. Layout Blueprint (Master–Detail Workspace)

```
┌──────────────────────┬───────────────────────────────────────────────────────────────┐
│ TARGETS (Sidebar)    │ TOP HEADER: Target Identity + Horizontal Stage Pipeline       │
│ [+ Launch Practice]  │ [BOOT] ──► [RESEARCH] ──► [ENGAGE] ──► [WIN]                  │
│ [Search] [Stage ▾]   │ KPI Strip: Touches | Sessions | Suspicion | Engagement | Conf │
│ ┌──────────────────┐ ├───────────────────────────────────────────────────────────────┤
│ │ ● Julius Muth    │ │ VERSION TIMELINE SCRUBBER                                     │
│ │   Co-CEO · revel8│ │ [v1 OSINT] ── [v4 Touch 1] ── [v9 Touch 2] ── ● [LIVE v14]   │
│ │   [WIN] 4/30     │ ├───────────────────────────────┬───────────────────────────────┤
│ │   Susp 0.14 ████ │ │ PANEL A: SUSPICION & SIGNALS  │ PANEL B: PERSONALITY EVOL.    │
│ └──────────────────┘ │ • Dual-line chart (Suspicion  │ • Psych Traits w/ Δ badges    │
│ ┌──────────────────┐ │   vs Engagement, 0.60 dashed  │   (0.55 → 0.85 +0.30 / NEW)   │
│ │ ● Tom Müller     │ │   pivot line + event pins)    │ • Evidence citation popovers  │
│ │   [ENGAGE] 3/30  │ │ • Latest Mood & Proposed Next │ • Style & Trust Guide Diff    │
│ │   Susp 0.42 ████ │ │ • Observed Watchouts (red     │ • Stressors & Hook Leverage   │
│ └──────────────────┘ │   flags + turn introduced)    │   Ranking                     │
│ ┌──────────────────┐ ├───────────────────────────────┴───────────────────────────────┤
│ │ ● Robert Seilbeck│ │ PANEL C: ACTIVITY & AUDIT LOG (Tabs)                          │
│ │   [ENGAGE] 2/10  │ │ [Conversation & Events]  [Orchestrator Decisions]  [Facts]    │
│ │   Susp 0.78 ████ │ │ • Interleaved Outbound/Inbound messages + inline signal pills │
│ └──────────────────┘ │ • Click any turn to jump Scrubber to that snapshot version    │
└──────────────────────┴───────────────────────────────────────────────────────────────┘
```

---

## 5. Component Inventory (`frontend/src/components/`)

### 5.1 Base Primitives (`components/ui/` — `shadcn/ui` style)
Owned directly in the repository for full Tailwind control:
- `badge.tsx` — stage pills, risk level pills, OSINT grade (`A/B/C/D`), channel chips
- `button.tsx` — primary CTA ("Launch Practice Run"), icon buttons, refresh, "Jump to Live", filter triggers
- `card.tsx` — consistent bordered panel container (`Card`, `CardHeader`, `CardTitle`, `CardContent`)
- `dialog.tsx` — modal dialog for launching a practice run
- `input.tsx` — sidebar target search & optional custom target ID / mock URL input
- `progress.tsx` — touch budget bar & trait confidence bar
- `scroll-area.tsx` — independent scrolling for sidebar & feed panels
- `select.tsx` — target selector (`julius`, `tom`, `robert`), objective selector, mode selector (`live` vs `mock/stub`)
- `separator.tsx` — subtle `1px` dividers
- `slider.tsx` — interactive snapshot version timeline scrubber
- `tabs.tsx` — Panel C tabs (`Conversation`, `Decisions`, `Facts`)
- `tooltip.tsx` — hover details on stage transitions, event markers, and `fact_id` citations

### 5.2 Domain Components (`components/dashboard/`)
- `TargetSidebar.tsx` — Top **"New Practice Run"** and amber **"Run Eval"** CTAs (replaced by **"Run Offline Replay"** in `--viewer` mode), target search, mock-run toggle, `Eval` scenario badges, active-stage / elapsed-time indicators, and live SSE sync status.
- `LaunchPracticeDialog.tsx` — `Dialog` for launching sandboxed practice runs (`julius`, `tom`, `robert`, or custom ID) with `zero_pacing` toggle via `POST /api/runs/launch`.
- `LaunchEvalDialog.tsx` — Guarded **Run Eval** `Dialog` placed directly under **New Practice Run**:
  - Reads local `GET /api/eval/status` on open (zero Target API calls) to display `remaining/15` tries left and per-persona counts (`5` tries max per persona, best 2 scored).
  - On-demand **Load Eval Targets** button calls read-only `GET /api/eval/scenarios` (`GET /v1/scenarios`, 0 sessions/touches spent) to reveal the 3 eval personas and allow picking 1–3 targets (or running 1 try on all eligible personas / manual IDs).
  - Requires explicit confirmation checkbox and enforces `--max-sessions 1` per launched eval process via `POST /api/eval/launch`.
- `RunExecutionStatus.tsx` — Live per-run execution telemetry strip showing active running stages (with ticking elapsed timer), completed stage timings (`stage_timing`), aggregate per-stage latency breakdowns, total run duration, and clean `run_exit` reason (`completed`, `stopped`, `cycle_limit`, `service_failure`).
- `StagePipeline.tsx` — Horizontal stepper (`BOOT → RESEARCH → ENGAGE → WIN/DONE/ABORT`) showing completed/current/terminal states, transition timestamps, and reasons, paired with the **Stop Run** action (`POST /api/runs/{run_id}/stop`).
- `KpiStrip.tsx` — 5 compact metric tiles: Touches (`used/max`), Sessions (`used/allowed`), Suspicion (`0.00–1.00` + risk badge), Engagement (`0.00–1.00`), and Profile Confidence (`%`).
- `VersionScrubber.tsx` — Milestone slider + quick-jump pills (`v1 Seed`, `Touch N`, `Pivot`, `Live`) that synchronizes Panels A, B, and C to the selected snapshot version (with a clear banner when inspecting historical state + one-click **"Return to Live"** button).
- `SuspicionChart.tsx` — Recharts `ComposedChart` / `LineChart`:
  - `suspicion` (`#F43F5E`, 2px stroke + soft rose area fill)
  - `engagement` (`#38BDF8`, 2px stroke)
  - `ReferenceLine y={0.6}` labeled `"Pivot Threshold (0.60)"`
  - Vertical reference markers for `strategy pivot`, `new_session`, `page.submitted`
  - Custom dark tooltip showing touch #, channel, mood, `proposed_next`, and signal `reason`
- `WatchoutsCard.tsx` — Active suspicion triggers (`profile.watchouts`) tagged with the snapshot version / touch where each watchout first appeared.
- `PersonalityEvolutionPanel.tsx` — Core personality view:
  - **Trait Evolution List:** Each `PsychTrait` shows initial confidence → selected-version confidence, delta badge (`+0.25`, `-0.10`, `NEW`), progress bar, and clickable `fact_id` evidence chips revealing source URL + OSINT grade.
  - **Style & Trust Guide Diff:** Displays current `style_guide` & `trust_style` alongside a toggleable diff against the initial OSINT/bootstrap profile so personality drift is immediately visible.
  - **Stressors & Hooks:** `profile.stressors` + ranked `hooks` with `engagement.score`, `touches_touched`, `positive`, and `candidate / active / exhausted` badges.
- `ActivityTabs.tsx` — Bottom full-width tabbed viewer:
  - **Conversation & Events:** Directional bubbles (`OUT` right-aligned subtle indigo tint with channel/subject/`facts_used`; `IN` left-aligned zinc surface with inline `suspicion`/`engagement` pill; system milestones like `register_page`, `link.opened`, `page.submitted`, `attachment.opened` as centered event dividers).
  - **Orchestrator Decisions:** Chronological decision log with op chips (`commit`, `reject`, `trigger`, `send`, `register_page`, `declare_incident`, `new_session`) and rationale text.
  - **Facts & Provenance:** Searchable table distinguishing `active` vs `superseded` (struck-through with `superseded_by` pointer) facts, `osint_grade`, `provenance`, `confidence`, and clickable source link.

---

## 6. Interaction & Live-Sync Rules

1. **Non-blocking live updates:** Frontend subscribes to `GET /api/stream` (SSE) with automatic fallback to `2.5s` polling. When the user is pinned to **Live**, new touches, suspicion spikes, and personality updates appear smoothly without resetting scroll position.
2. **Historical scrubbing lock:** If the user scrubs to an earlier snapshot version `vK < latest`, live updates still update the sidebar and increment the "Live (vN)" button badge without yanking the user away from `vK`.
3. **Click-to-inspect cross-highlighting:** Clicking any turn in **Conversation** or any point on the **Suspicion Chart** jumps the scrubber to that exact snapshot version so Panels A and B immediately reflect the target's suspicion and personality at that moment.
4. **Empty / Loading / Offline states:** Clear skeleton loaders while fetching, helpful empty state when `data/` has no `.db` files yet (showing the exact CLI command to run a campaign or `--seed-demo`), and a subtle amber banner if the backend connection drops.

---

## 7. Accessibility & Consistency Guardrails

- Minimum `4.5:1` text contrast on all `#111114` cards (`zinc-50` / `zinc-200` / `zinc-400` verified).
- Never rely on color alone for risk or stage: every suspicion badge pairs color with numeric score (`0.78`) and label (`CRITICAL`), and every stage uses both icon and text.
- Keyboard navigable sidebar, tabs, and timeline scrubber (`ArrowLeft` / `ArrowRight` steps through snapshot versions).
- Consistent naming: use `Target` / `Campaign` / `Stage` / `Suspicion` / `Personality Evolution` consistently across all labels and API fields.
