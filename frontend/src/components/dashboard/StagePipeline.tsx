"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import type { CampaignState, StageTransition, TargetSummary } from "@/lib/types";
import { cn, getStageTextClass } from "@/lib/utils";

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

  return (
    <header className="flex flex-col gap-4 border-b border-zinc-800/80 pb-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2.5">
            <h1 className="text-xl font-semibold tracking-tight text-zinc-50">
              {summary.target_name || summary.target_id}
            </h1>
            <span className={cn("font-mono text-xs font-medium", getStageTextClass(activeState))}>
              {activeState}
            </span>
          </div>
          <p className="mt-0.5 text-xs text-zinc-400">
            {[summary.target_role, summary.target_org].filter(Boolean).join(" · ") || summary.run_id}
          </p>
        </div>

        <div className="flex items-center gap-3">
          {summary.sender_name && (
            <div className="flex items-center gap-2.5">
              <div className="text-right">
                <p className="text-xs text-zinc-300">{summary.sender_name}</p>
                {summary.sender_domain && (
                  <p className="font-mono text-[11px] text-zinc-500">{summary.sender_domain}</p>
                )}
              </div>
              {portraitUrl && (
                /* eslint-disable-next-line @next/next/no-img-element */
                <img
                  src={portraitUrl}
                  alt={summary.sender_name}
                  onError={() => setPortraitFailed(true)}
                  className="h-8 w-8 rounded-full border border-zinc-700 object-cover"
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
      <ol className="flex flex-wrap items-center gap-2 text-xs">
        {steps.map((step, idx) => {
          const isReached = idx <= activeIdx;
          const isCurrent = idx === activeIdx;
          const transition = reachedMap.get(step);

          return (
            <li key={step} className="flex items-center gap-2">
              {idx > 0 && (
                <span
                  className={cn("h-px w-6", isReached ? "bg-zinc-500" : "bg-zinc-800")}
                  aria-hidden="true"
                />
              )}
              <span
                title={transition?.reason || step}
                className={cn(
                  "inline-flex items-center gap-1.5 font-mono text-[11px]",
                  isCurrent
                    ? step === "WIN"
                      ? "font-semibold text-emerald-400"
                      : step === "ABORT"
                        ? "font-semibold text-rose-400"
                        : "font-semibold text-zinc-50"
                    : isReached
                      ? "text-zinc-400"
                      : "text-zinc-600"
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
                {step}
              </span>
            </li>
          );
        })}
      </ol>
    </header>
  );
}
