"use client";

import { CheckCircle2, ChevronDown, ChevronRight, Clock, Loader2, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import type { CampaignState, ExecutionStatus } from "@/lib/types";
import { cn, formatDuration, formatTimeShort } from "@/lib/utils";

interface RunExecutionStatusProps {
  executionStatus?: ExecutionStatus;
  state: CampaignState;
}

const STAGE_LABELS: Record<string, string> = {
  osint: "OSINT",
  identity: "Identity",
  profile: "Profile",
  strategy: "Strategy",
  messaging: "Messaging",
  qa: "QA Gate",
  target_api: "Target API",
};

function labelForStage(stage: string): string {
  return STAGE_LABELS[stage] ?? stage.replaceAll("_", " ");
}

function liveElapsedMs(startedAt: string | null | undefined, fallbackMs: number, nowMs: number): number {
  if (!startedAt) return fallbackMs;
  const parsed = Date.parse(startedAt);
  if (Number.isNaN(parsed)) return fallbackMs;
  return Math.max(fallbackMs, nowMs - parsed);
}

export function RunExecutionStatus({ executionStatus, state }: RunExecutionStatusProps) {
  const [traceOpen, setTraceOpen] = useState(false);
  const [nowMs, setNowMs] = useState<number>(() => Date.now());

  const isRunning = Boolean(
    executionStatus?.is_running || (executionStatus?.active_steps?.length ?? 0) > 0
  );

  // Tick live elapsed timers smoothly while any stage or run is active
  useEffect(() => {
    if (!isRunning) return;
    setNowMs(Date.now());
    const id = setInterval(() => setNowMs(Date.now()), 500);
    return () => clearInterval(id);
  }, [isRunning]);

  if (!executionStatus) return null;

  const activeSteps = executionStatus.active_steps ?? [];
  const completedSteps = executionStatus.completed_steps ?? [];
  const stageTotals = executionStatus.stage_totals ?? [];
  const failedCount = completedSteps.filter((s) => !s.ok).length;

  const totalRunMs = isRunning
    ? liveElapsedMs(executionStatus.started_at, executionStatus.total_elapsed_ms, nowMs)
    : executionStatus.total_elapsed_ms;

  const maxStageTotalMs = Math.max(
    1,
    ...stageTotals.map((s) => s.total_ms),
    ...activeSteps.map((a) => liveElapsedMs(a.started_at, a.elapsed_ms, nowMs))
  );

  const hasInferredOnly =
    completedSteps.length > 0 && completedSteps.every((s) => s.inferred === true);

  return (
    <section className="rounded-xl border border-zinc-800 bg-[#111114] p-4">
      {/* Top status bar */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2.5">
          <span
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 font-mono text-[11px] font-medium",
              isRunning
                ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-400"
                : state === "WIN"
                  ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-400"
                  : state === "ABORT" || executionStatus.exit_reason === "service_failure"
                    ? "border-rose-500/30 bg-rose-500/10 text-rose-400"
                    : "border-zinc-700 bg-zinc-900 text-zinc-300"
            )}
          >
            {isRunning ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : state === "ABORT" || executionStatus.exit_reason === "service_failure" ? (
              <XCircle className="h-3 w-3" />
            ) : (
              <CheckCircle2 className="h-3 w-3" />
            )}
            {isRunning
              ? "RUNNING"
              : executionStatus.exit_reason
                ? executionStatus.exit_reason.toUpperCase()
                : state}
          </span>

          <span className="text-xs font-medium text-zinc-200">
            {isRunning && activeSteps.length > 0
              ? `Running ${activeSteps
                  .map((s) => (s.mode ? `${labelForStage(s.stage)} (${s.mode})` : labelForStage(s.stage)))
                  .join(" + ")}`
              : isRunning
                ? "Orchestrator active (between stages)"
                : `Finished · ${completedSteps.length} step${completedSteps.length === 1 ? "" : "s"}`}
          </span>

          {failedCount > 0 && (
            <span className="font-mono text-[11px] text-rose-400">
              {failedCount} failed
            </span>
          )}
        </div>

        <div className="flex items-center gap-3 font-mono text-xs tabular-nums">
          <span className="inline-flex items-center gap-1.5 text-zinc-300" title="Total run duration">
            <Clock className="h-3.5 w-3.5 text-zinc-500" />
            {formatDuration(totalRunMs)}
          </span>
          {completedSteps.length > 0 && (
            <button
              type="button"
              onClick={() => setTraceOpen((prev) => !prev)}
              className="inline-flex cursor-pointer items-center gap-1 rounded-md border border-zinc-800 bg-zinc-900/70 px-2 py-1 text-[11px] text-zinc-400 transition-colors hover:border-zinc-700 hover:text-zinc-100"
            >
              {traceOpen ? (
                <ChevronDown className="h-3 w-3" />
              ) : (
                <ChevronRight className="h-3 w-3" />
              )}
              Steps ({completedSteps.length + activeSteps.length})
            </button>
          )}
        </div>
      </div>

      {/* Currently running stages (live ticking) */}
      {activeSteps.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {activeSteps.map((step) => {
            const elapsed = liveElapsedMs(step.started_at, step.elapsed_ms, nowMs);
            return (
              <div
                key={step.step_id}
                className="flex items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-3 py-1.5 font-mono text-xs tabular-nums"
              >
                <Loader2 className="h-3.5 w-3.5 animate-spin text-emerald-400" />
                <span className="font-medium text-zinc-100">{labelForStage(step.stage)}</span>
                {step.mode && <span className="text-zinc-400">{step.mode}</span>}
                <span className="text-emerald-400">{formatDuration(elapsed)}</span>
              </div>
            );
          })}
        </div>
      )}

      {/* Per-stage duration summary chips */}
      {stageTotals.length > 0 && (
        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
          {stageTotals.map((st) => {
            const pct = Math.max(4, Math.min(100, Math.round((st.total_ms / maxStageTotalMs) * 100)));
            const isStageActive = activeSteps.some((a) => a.stage === st.stage);
            return (
              <div
                key={st.stage}
                className={cn(
                  "rounded-lg border px-2.5 py-2",
                  isStageActive
                    ? "border-emerald-500/40 bg-zinc-900/80"
                    : "border-zinc-800/80 bg-zinc-950/60"
                )}
              >
                <div className="flex items-center justify-between gap-1">
                  <span className="truncate text-[11px] font-medium text-zinc-300">
                    {labelForStage(st.stage)}
                  </span>
                  <span className="font-mono text-[10px] tabular-nums text-zinc-500">
                    ×{st.calls}
                  </span>
                </div>
                <div className="mt-1 flex items-baseline justify-between gap-1 font-mono tabular-nums">
                  <span className="text-xs font-semibold text-zinc-100">
                    {formatDuration(st.total_ms)}
                  </span>
                  {st.failures > 0 ? (
                    <span className="text-[10px] text-rose-400">{st.failures} err</span>
                  ) : (
                    <span className="text-[10px] text-zinc-500">
                      avg {formatDuration(st.avg_ms)}
                    </span>
                  )}
                </div>
                <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-zinc-800">
                  <div
                    className={cn(
                      "h-full rounded-full",
                      st.failures > 0
                        ? "bg-rose-500/80"
                        : isStageActive
                          ? "bg-emerald-400"
                          : "bg-zinc-400"
                    )}
                    style={{ width: `${pct}%` }}
                  />
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Collapsible step-by-step execution trace */}
      {traceOpen && (completedSteps.length > 0 || activeSteps.length > 0) && (
        <div className="mt-3 max-h-60 overflow-y-auto rounded-lg border border-zinc-800/80 bg-zinc-950/70">
          {hasInferredOnly && (
            <p className="border-b border-zinc-800/80 px-3 py-1.5 text-[11px] text-zinc-500">
              Durations inferred from event timestamps (legacy DB recorded before explicit stage timings).
            </p>
          )}
          <table className="w-full text-left font-mono text-[11px] tabular-nums">
            <thead className="sticky top-0 bg-zinc-950 text-[10px] uppercase tracking-wider text-zinc-500">
              <tr className="border-b border-zinc-800/80">
                <th className="px-3 py-1.5 font-medium">#</th>
                <th className="px-3 py-1.5 font-medium">Stage</th>
                <th className="px-3 py-1.5 font-medium">Mode</th>
                <th className="px-3 py-1.5 font-medium">Status</th>
                <th className="px-3 py-1.5 font-medium">Time</th>
                <th className="px-3 py-1.5 text-right font-medium">Duration</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800/50">
              {completedSteps.map((step, idx) => (
                <tr key={`${step.step_id}_${idx}`} className="hover:bg-zinc-900/50">
                  <td className="px-3 py-1.5 text-zinc-500">{idx + 1}</td>
                  <td className="px-3 py-1.5 text-zinc-200">{labelForStage(step.stage)}</td>
                  <td className="px-3 py-1.5 text-zinc-400">{step.mode || "—"}</td>
                  <td className="px-3 py-1.5">
                    <span className={step.ok ? "text-emerald-400" : "text-rose-400"}>
                      {step.ok ? "OK" : "FAIL"}
                    </span>
                  </td>
                  <td className="px-3 py-1.5 text-zinc-500">{formatTimeShort(step.finished_at)}</td>
                  <td className="px-3 py-1.5 text-right text-zinc-200">
                    {formatDuration(step.elapsed_ms)}
                  </td>
                </tr>
              ))}
              {activeSteps.map((step, idx) => (
                <tr key={`active_${step.step_id}_${idx}`} className="bg-emerald-500/5">
                  <td className="px-3 py-1.5 text-zinc-500">{completedSteps.length + idx + 1}</td>
                  <td className="px-3 py-1.5 font-medium text-zinc-100">
                    {labelForStage(step.stage)}
                  </td>
                  <td className="px-3 py-1.5 text-zinc-400">{step.mode || "—"}</td>
                  <td className="px-3 py-1.5 text-emerald-400">RUNNING</td>
                  <td className="px-3 py-1.5 text-zinc-500">{formatTimeShort(step.started_at)}</td>
                  <td className="px-3 py-1.5 text-right font-medium text-emerald-400">
                    {formatDuration(liveElapsedMs(step.started_at, step.elapsed_ms, nowMs))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
