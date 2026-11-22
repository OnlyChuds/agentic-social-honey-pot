"use client";

import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import type { CampaignState, ExecutionStatus } from "@/lib/types";
import {
  cn,
  formatAgentStage,
  formatDuration,
  formatExitReason,
  formatTimeShort,
  titleCaseSnake,
} from "@/lib/utils";

interface RunExecutionStatusProps {
  executionStatus?: ExecutionStatus;
  state: CampaignState;
}

function liveElapsedMs(
  startedAt: string | null | undefined,
  fallbackMs: number,
  nowMs: number
): number {
  if (!startedAt) return fallbackMs;
  const parsed = Date.parse(startedAt);
  if (Number.isNaN(parsed)) return fallbackMs;
  return Math.max(fallbackMs, nowMs - parsed);
}

export function RunExecutionStatus({ executionStatus, state }: RunExecutionStatusProps) {
  const [expanded, setExpanded] = useState(false);
  const [nowMs, setNowMs] = useState<number>(() => Date.now());

  const isRunning = Boolean(
    executionStatus?.is_running || (executionStatus?.active_steps?.length ?? 0) > 0
  );

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
  const totalStepCount = completedSteps.length + activeSteps.length;

  const totalRunMs = isRunning
    ? liveElapsedMs(executionStatus.started_at, executionStatus.total_elapsed_ms, nowMs)
    : executionStatus.total_elapsed_ms;

  const maxStageTotalMs = Math.max(
    1,
    ...stageTotals.map((s) => s.total_ms),
    ...activeSteps.map((a) => liveElapsedMs(a.started_at, a.elapsed_ms, nowMs))
  );

  const isErrorExit = state === "ABORT" || executionStatus.exit_reason === "service_failure";
  const statusText = isRunning
    ? activeSteps.length > 0
      ? `Running ${activeSteps.map((s) => formatAgentStage(s.stage)).join(" + ")}`
      : "Orchestrator active"
    : formatExitReason(executionStatus.exit_reason, state);

  return (
    <section className="rounded-xl border border-zinc-800/80 bg-card px-5 py-3">
      {/* Compact single-line execution bar */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2.5 text-xs">
          {isRunning ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-emerald-400" />
          ) : (
            <span
              className={cn(
                "h-1.5 w-1.5 rounded-full",
                state === "WIN"
                  ? "bg-emerald-400"
                  : isErrorExit
                    ? "bg-rose-400"
                    : "bg-zinc-400"
              )}
            />
          )}

          <span
            className={cn(
              "font-medium",
              isRunning || state === "WIN"
                ? "text-emerald-400"
                : isErrorExit
                  ? "text-rose-400"
                  : "text-zinc-200"
            )}
          >
            {statusText}
          </span>

          <span className="text-zinc-600">·</span>
          <span className="text-zinc-400">
            <span className="font-mono tabular-nums text-zinc-300">{totalStepCount}</span>{" "}
            {totalStepCount === 1 ? "agent step" : "agent steps"}
          </span>

          {failedCount > 0 && (
            <>
              <span className="text-zinc-600">·</span>
              <span className="text-rose-400">
                <span className="font-mono tabular-nums">{failedCount}</span> failed
              </span>
            </>
          )}
        </div>

        <div className="flex items-center gap-4 text-xs">
          <span className="font-mono tabular-nums text-zinc-300">
            {formatDuration(totalRunMs)}
          </span>
          {(stageTotals.length > 0 || totalStepCount > 0) && (
            <button
              type="button"
              onClick={() => setExpanded((prev) => !prev)}
              className="inline-flex cursor-pointer items-center gap-1 text-xs text-zinc-400 transition-colors hover:text-zinc-100"
            >
              {expanded ? (
                <ChevronDown className="h-3.5 w-3.5" />
              ) : (
                <ChevronRight className="h-3.5 w-3.5" />
              )}
              <span>{expanded ? "Hide trace" : "Execution trace"}</span>
            </button>
          )}
        </div>
      </div>

      {/* Live running stage strip (only visible while actively running) */}
      {isRunning && activeSteps.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2 border-t border-zinc-800/80 pt-3">
          {activeSteps.map((step) => {
            const elapsed = liveElapsedMs(step.started_at, step.elapsed_ms, nowMs);
            return (
              <div
                key={step.step_id}
                className="flex items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-3 py-1.5 text-xs"
              >
                <Loader2 className="h-3 w-3 animate-spin text-emerald-400" />
                <span className="font-medium text-zinc-100">{formatAgentStage(step.stage)}</span>
                {step.mode && <span className="text-zinc-400">{titleCaseSnake(step.mode)}</span>}
                <span className="font-mono tabular-nums text-emerald-400">
                  {formatDuration(elapsed)}
                </span>
              </div>
            );
          })}
        </div>
      )}

      {/* On-demand expanded breakdown + full step trace */}
      {expanded && (
        <div className="mt-4 space-y-4 border-t border-zinc-800/80 pt-4">
          {stageTotals.length > 0 && (
            <div className="grid grid-cols-3 gap-x-6 gap-y-3">
              {stageTotals.map((st) => {
                const pct = Math.max(
                  3,
                  Math.min(100, Math.round((st.total_ms / maxStageTotalMs) * 100))
                );
                const isStageActive = activeSteps.some((a) => a.stage === st.stage);
                return (
                  <div key={st.stage} className="space-y-1.5">
                    <div className="flex items-baseline justify-between gap-2 text-xs">
                      <span className="font-medium text-zinc-200">
                        {formatAgentStage(st.stage)}
                        <span className="ml-1.5 font-mono text-[11px] font-normal tabular-nums text-zinc-400">
                          ×{st.calls}
                        </span>
                      </span>
                      <span className="font-mono tabular-nums text-zinc-300">
                        {formatDuration(st.total_ms)}
                        <span className="ml-2 text-[11px] text-zinc-400">
                          avg {formatDuration(st.avg_ms)}
                        </span>
                      </span>
                    </div>
                    <div className="h-1 w-full overflow-hidden rounded-full bg-zinc-800">
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

          {(completedSteps.length > 0 || activeSteps.length > 0) && (
            <div className="max-h-64 overflow-x-auto overflow-y-auto rounded-lg border border-zinc-800/80 bg-background/60">
              <table className="w-full text-left text-xs">
                <thead className="sticky top-0 bg-background text-[11px] text-zinc-400">
                  <tr className="border-b border-zinc-800/80">
                    <th className="px-3 py-2 font-medium">#</th>
                    <th className="px-3 py-2 font-medium">Agent stage</th>
                    <th className="px-3 py-2 font-medium">Task</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="px-3 py-2 font-medium">Time</th>
                    <th className="px-3 py-2 text-right font-medium">Duration</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800/50">
                  {completedSteps.map((step, idx) => (
                    <tr key={`${step.step_id}_${idx}`} className="hover:bg-card-elevated/50">
                      <td className="px-3 py-1.5 font-mono tabular-nums text-zinc-400">
                        {idx + 1}
                      </td>
                      <td className="px-3 py-1.5 text-zinc-200">
                        {formatAgentStage(step.stage)}
                      </td>
                      <td className="px-3 py-1.5 text-zinc-400">
                        {step.mode ? titleCaseSnake(step.mode) : "—"}
                      </td>
                      <td className="px-3 py-1.5">
                        <span className={step.ok ? "text-emerald-400" : "text-rose-400"}>
                          {step.ok ? "OK" : "Failed"}
                        </span>
                      </td>
                      <td className="px-3 py-1.5 font-mono tabular-nums text-zinc-400">
                        {formatTimeShort(step.finished_at)}
                      </td>
                      <td className="px-3 py-1.5 text-right font-mono tabular-nums text-zinc-200">
                        {formatDuration(step.elapsed_ms)}
                      </td>
                    </tr>
                  ))}
                  {activeSteps.map((step, idx) => (
                    <tr key={`active_${step.step_id}_${idx}`} className="bg-emerald-500/5">
                      <td className="px-3 py-1.5 font-mono tabular-nums text-zinc-400">
                        {completedSteps.length + idx + 1}
                      </td>
                      <td className="px-3 py-1.5 font-medium text-zinc-100">
                        {formatAgentStage(step.stage)}
                      </td>
                      <td className="px-3 py-1.5 text-zinc-400">
                        {step.mode ? titleCaseSnake(step.mode) : "—"}
                      </td>
                      <td className="px-3 py-1.5 text-emerald-400">Running</td>
                      <td className="px-3 py-1.5 font-mono tabular-nums text-zinc-400">
                        {formatTimeShort(step.started_at)}
                      </td>
                      <td className="px-3 py-1.5 text-right font-mono font-medium tabular-nums text-emerald-400">
                        {formatDuration(liveElapsedMs(step.started_at, step.elapsed_ms, nowMs))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
