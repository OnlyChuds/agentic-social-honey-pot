"use client";

import { Button } from "@/components/ui/button";
import { Slider } from "@/components/ui/slider";
import type { PersonalityMilestone } from "@/lib/types";
import { formatTimeShort } from "@/lib/utils";

interface VersionScrubberProps {
  milestones: PersonalityMilestone[];
  selectedIndex: number;
  isLivePinned: boolean;
  onSelectIndex: (index: number) => void;
  onJumpLive: () => void;
}

export function VersionScrubber({
  milestones,
  selectedIndex,
  isLivePinned,
  onSelectIndex,
  onJumpLive,
}: VersionScrubberProps) {
  if (milestones.length <= 1) return null;

  const maxIdx = milestones.length - 1;
  const clampedIdx = Math.min(maxIdx, Math.max(0, selectedIndex));
  const current = milestones[clampedIdx];

  return (
    <div className="flex flex-wrap items-center gap-4 rounded-xl border border-zinc-800/80 bg-[#111114] px-4 py-3">
      <div className="flex min-w-[180px] items-baseline gap-2">
        <span className="text-xs font-medium text-zinc-300">Timeline</span>
        <span className="font-mono text-xs text-zinc-400">
          v{current.version} · {current.label}
        </span>
      </div>

      <div className="min-w-[160px] flex-1">
        <Slider
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

      <div className="flex items-center gap-3">
        <span className="font-mono text-[11px] text-zinc-500">
          {formatTimeShort(current.ts)}
        </span>
        {isLivePinned ? (
          <span className="font-mono text-[11px] text-emerald-400">Live</span>
        ) : (
          <Button size="sm" variant="outline" onClick={onJumpLive} className="h-6 px-2 text-[11px]">
            Back to Live
          </Button>
        )}
      </div>
    </div>
  );
}
