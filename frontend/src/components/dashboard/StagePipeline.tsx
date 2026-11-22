"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import type { CampaignState, StageTransition, TargetSummary } from "@/lib/types";
import { cn, formatRunTag, formatStageState, getStageTextClass } from "@/lib/utils";

interface StagePipelineProps {
  summary: TargetSummary;
  stageHistory: StageTransition[];
  activeState: CampaignState;
  onStopRun?: () => void;
  stopping?: boolean;
}

export function StagePipeline({
  summary,
  stageHistory,
  activeState,
  onStopRun,
  stopping,
}: StagePipelineProps) {
  const [portraitFailed, setPortraitFailed] = useState(false);
  useEffect(() => {
    setPortraitFailed(false);
  }, [summary.run_id, summary.portrait_asset_id]);

  const terminalStage: CampaignState =
    activeState === "DONE" || activeState === "ABORT" ? activeState : "WIN";
  const steps: CampaignState[] = ["BOOT", "RESEARCH", "ENGAGE", terminalStage];
  const activeIdx = Math.max(0, steps.indexOf(activeState));
  const reachedMap = new Map(stageHistory.map((s) => [s.stage, s]));
  const portraitUrl =
    summary.portrait_asset_id && !portraitFailed
      ? `/api/targets/${encodeURIComponent(summary.run_id)}/assets/${encodeURIComponent(
          summary.portrait_asset_id
        )}`
      : null;

  const runTag = formatRunTag(summary.run_id);
  const roleAlreadyHasOrg =
    Boolean(summary.target_role) &&
    Boolean(summary.target_org) &&
    summary.target_role.toLowerCase().includes(summary.target_org.toLowerCase());
  const subtitleParts = roleAlreadyHasOrg
    ? [summary.target_role]
    : [summary.target_role, summary.target_org].filter(Boolean);

  return (
    <header className="flex flex-col gap-3.5 border-b border-zinc-800/80 pb-4">
      <div className="flex items-start justify-between gap-6">
        <div className="min-w-0">
          <div className="flex items-baseline gap-2.5">
            <h1 className="text-2xl font-semibold tracking-tight text-zinc-50">
              {summary.target_name || summary.target_id}
            </h1>
            {runTag && <span className="text-sm text-zinc-400">{runTag}</span>}
            <span className={cn("text-sm font-medium", getStageTextClass(activeState))}>
              · {formatStageState(activeState)}
            </span>
          </div>
          <p className="mt-1 text-sm text-zinc-400">
            {subtitleParts.join(" · ") || summary.run_id}
          </p>
        </div>

        <div className="flex shrink-0 items-center gap-4">
          {summary.sender_name && (
            <div className="flex items-center gap-3">
              <div className="text-right">
                <p className="text-[11px] text-zinc-400">AI Persona</p>
                <p className="text-sm font-medium text-zinc-200">{summary.sender_name}</p>
                {summary.sender_domain && (
                  <p className="text-xs text-zinc-400">{summary.sender_domain}</p>
                )}
              </div>
              {portraitUrl && (
                /* eslint-disable-next-line @next/next/no-img-element */
                <img
                  src={portraitUrl}
                  alt={summary.sender_name}
                  onError={() => setPortraitFailed(true)}
                  className="h-10 w-10 rounded-full border border-zinc-800 object-cover object-top"
                />
              )}
            </div>
          )}
          {summary.is_running && onStopRun && (
            <Button variant="outline" size="sm" onClick={onStopRun} disabled={stopping}>
              {stopping ? "Stopping..." : "Stop Run"}
            </Button>
          )}
        </div>
      </div>

      {/* Minimal inline stage track */}
      <ol className="flex items-center gap-2.5 text-xs">
        {steps.map((step, idx) => {
          const isReached = idx <= activeIdx;
          const isCurrent = idx === activeIdx;
          const transition = reachedMap.get(step);

          return (
            <li key={step} className="flex items-center gap-2.5">
              {idx > 0 && (
                <span
                  className={cn("h-px w-8", isReached ? "bg-zinc-600" : "bg-zinc-800")}
                  aria-hidden="true"
                />
              )}
              <span
                title={transition?.reason || formatStageState(step)}
                className={cn(
                  "inline-flex items-center gap-1.5 text-xs",
                  isCurrent
                    ? step === "WIN"
                      ? "font-medium text-emerald-400"
                      : step === "ABORT"
                        ? "font-medium text-rose-400"
                        : "font-medium text-zinc-100"
                    : isReached
                      ? "text-zinc-400"
                      : "text-zinc-500"
                )}
              >
                <span
                  className={cn(
                    "h-1.5 w-1.5 rounded-full",
                    isCurrent
                      ? step === "WIN"
                        ? "bg-emerald-400"
                        : step === "ABORT"
                          ? "bg-rose-400"
                          : "bg-zinc-100"
                      : isReached
                        ? "bg-zinc-500"
                        : "bg-zinc-800"
                  )}
                />
                {formatStageState(step)}
              </span>
            </li>
          );
        })}
      </ol>
    </header>
  );
}
