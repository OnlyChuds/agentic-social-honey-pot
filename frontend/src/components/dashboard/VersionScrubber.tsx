"use client";

import { Button } from "@/components/ui/button";
import { Slider } from "@/components/ui/slider";
import type { PersonalityMilestone } from "@/lib/types";
import { cn, formatMilestoneLabel, formatTimeShort } from "@/lib/utils";

interface VersionScrubberProps {
  milestones: PersonalityMilestone[];
  selectedIndex: number;
  isLivePinned: boolean;
  isRunning?: boolean;
  onSelectIndex: (index: number) => void;
  onJumpLive: () => void;
}

export function VersionScrubber({
  milestones,
  selectedIndex,
  isLivePinned,
  isRunning = false,
  onSelectIndex,
  onJumpLive,
}: VersionScrubberProps) {
  if (milestones.length <= 1) return null;

  const maxIdx = milestones.length - 1;
  const clampedIdx = Math.min(maxIdx, Math.max(0, selectedIndex));
  const current = milestones[clampedIdx];
  const latest = milestones[maxIdx];

  return (
    <div
      className={cn(
        "flex items-center gap-5 rounded-xl border bg-card px-5 py-3 transition-colors",
        isLivePinned ? "border-zinc-800/80" : "border-zinc-700"
      )}
    >
      <div className="flex min-w-[210px] items-baseline gap-2 text-xs">
        <span className="font-medium text-zinc-200">
          {isLivePinned ? "Timeline" : "Historical snapshot"}
        </span>
        <span className="font-mono tabular-nums text-zinc-300">v{current.version}</span>
        {!isLivePinned && (
          <span className="font-mono text-[11px] tabular-nums text-zinc-500">
            / v{latest.version}
          </span>
        )}
        <span className="truncate text-zinc-400">· {formatMilestoneLabel(current.label)}</span>
      </div>

      <div className="flex-1">
        <Slider
          aria-label="Campaign snapshot timeline"
          min={0}
          max={maxIdx}
          step={1}
          value={[clampedIdx]}
          onValueChange={(vals) => {
            if (typeof vals[0] === "number") {
              onSelectIndex(vals[0]);
            }
          }}
        />
      </div>

      <div className="flex shrink-0 items-center gap-3 text-xs">
        <span className="font-mono tabular-nums text-zinc-400">
          {formatTimeShort(current.ts)}
        </span>
        {isLivePinned ? (
          <span className={cn("font-medium", isRunning ? "text-emerald-400" : "text-zinc-400")}>
            {isRunning ? "Live" : "Latest"}
          </span>
        ) : (
          <Button size="sm" variant="outline" onClick={onJumpLive} className="h-7 px-2.5 text-xs">
            Return to latest
          </Button>
        )}
      </div>
    </div>
  );
}
