# Experiment review — 2026-11-21

The latest observed practice run spent **6m 01s before its first outbound turn**.
It recorded five outbound turns and no target events, but no terminal state. That
is an observation, not a completed no-reply result. The local database was still
being written when the analysis began.

| Database | Last recorded state | Touches | Replies | Start to first outbound | What the log shows |
| --- | --- | ---: | ---: | ---: | --- |
| `practice_robert_run3.db` | `ENGAGE` | 5 | 0 | 6m 01s | No target events or recorded errors at the sampled point. |
| `practice_julius_run16.db` | `ENGAGE` | 4 | 1 | 6m 05s | Nine QA verdicts, four failures. The gap between outbound turns two and three was 7m 21s, with several QA events in that interval. The event log cannot attribute the whole gap to QA. |
| `practice_julius_run15.db` | `ABORT` | 2 | 1 | 4m 21s | The messaging component logged an HTTP 404 immediately before the abort. The log does not establish the root cause. |

In the latest run, the first OSINT and identity results were recorded together
at +3m 17s, the profile result at +4m 24s, and the initial plan at +5m 36s.
These are recorded milestones, not per-component timings. Earlier databases
include subsecond runs without reliable execution-mode labels, so pooling them
with these runs would make a single completion or reply rate misleading.

## What to improve first

1. **Label execution mode and exit reason.** Store a run manifest with fixture
   vs. practice mode, code revision and dirty flag, non-secret config fingerprint,
   fixture version, and run ID. Record exit reasons separately from state:
   normal completion, cycle limit, operator stop, service failure, or unknown.
   The loop can hit `max_cycles` without recording a terminal state, so `ENGAGE`
   alone cannot distinguish a running process from one that already stopped.
2. **Time stages rather than inferring them from gaps.** Capture start/end,
   elapsed time, attempt number, and coarse failure category for each stage.
   Keep three distinct records for a local mock transport: request acknowledged
   with no events, request acknowledged with events, and outcome unknown. Don't
   persist message bodies, credentials, endpoint URLs, or raw error strings in
   metrics.
3. **Start with a controlled offline baseline.** Use neutral canned messages and
   fixed mock responses, no external network, and a fresh database per run. Replay
   cases for no events, one reply, a noisy second event, a QA rejection, a 404,
   a timeout, and a cycle limit. Change one factor at a time. Compare expected
   classifications and stage timing on identical fixtures; report infrastructure
   failures and incomplete observations separately.

The first thing to test is whether the same mock reply and empty-response fixtures
are classified correctly on every replay. Then measure where time goes before
deciding what to change. Raw reply counts from the saved runs are not a controlled
comparison.

## Reproduce the summary

```sh
python3 scripts/summarize_runs.py \
  data/practice_robert_run3.db \
  data/practice_julius_run16.db \
  data/practice_julius_run15.db
```

The script opens each run in read-only mode, includes committed WAL records, and
outputs JSON without message bodies, session IDs, or error text. Counts cover
the recorded run, while `current_session_touches` comes from the latest snapshot
and can reset across sessions. `observed_event_span_seconds` is not a measure of
total runtime. A false `terminal_state_recorded` means no terminal state was
observed, not that the process is still active.

### Exit and error metadata

- `exit_reason` is separate from the last state. Offline fixtures put a
  `system` event with `kind: run_exit` and a bounded `reason` value at the
  end of the log. Recognized reasons are `completed`, `cycle_limit`,
  `operator_stop`, `service_failure`, and `unknown`. With no valid marker at
  the end of the log, the report returns `null` rather than guessing. The
  live runner does not write this marker.
- `error_categories` groups error records by a structured `category`, when
  present. For older logs it recognizes clear HTTP status and timeout prefixes
  and otherwise counts the error as `unknown`. No raw error text is exported.
  A status category is not a diagnosis of the underlying cause.

Run the offline reporting fixtures with `python -m pytest tests/unit/test_summarize_runs.py -q`.

### Dashboard offline replay

The viewer's **Run Offline Replay** button saves and opens a fresh database
containing a canned acknowledgment. It doesn't run agents or send requests to
the target service. Start the viewer backend from the project root:

```sh
uv run python -m src.web.server --viewer --data-dir data --port 8001
```

Then start the frontend from `frontend/` with
`FASTAPI_URL=http://127.0.0.1:8001 npm run dev`.
