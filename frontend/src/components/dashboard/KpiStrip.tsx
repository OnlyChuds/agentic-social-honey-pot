"use client";

import { cn, formatPercent, formatScore, getSuspicionBand } from "@/lib/utils";

interface KpiStripProps {
  touchesUsed: number;
  sessionMax: number;
  sessionsUsed: number;
  sessionsAllowed: number;
  suspicion: number | null;
  engagement: number | null;
  mood: string | null;
  profileConfidence: number;
  incidentsCount: number;
}

export function KpiStrip({
  touchesUsed,
  sessionMax,
  sessionsUsed,
  sessionsAllowed,
  suspicion,
  engagement,
  mood,
  profileConfidence,
  incidentsCount,
}: KpiStripProps) {
  const suspBand = getSuspicionBand(suspicion);

  return (
    <div className="grid grid-cols-2 gap-y-4 rounded-xl border border-zinc-800/80 bg-[#111114] px-5 py-4 sm:grid-cols-5 sm:divide-x sm:divide-zinc-800/80">
      <div className="sm:pr-4">
        <p className="text-[11px] text-zinc-500">Touches</p>
        <p className="mt-1 font-mono text-xl font-medium tabular-nums text-zinc-100">
          {touchesUsed}
          <span className="text-sm text-zinc-500">/{sessionMax}</span>
        </p>
        {incidentsCount > 0 && (
          <p className="mt-0.5 text-[11px] text-emerald-400">Objective met</p>
        )}
      </div>

      <div className="sm:px-4">
        <p className="text-[11px] text-zinc-500">Sessions</p>
        <p className="mt-1 font-mono text-xl font-medium tabular-nums text-zinc-100">
          {sessionsUsed}
          <span className="text-sm text-zinc-500">/{sessionsAllowed}</span>
        </p>
        {sessionsUsed > 1 && <p className="mt-0.5 text-[11px] text-zinc-400">Memory retained</p>}
      </div>

      <div className="sm:px-4">
        <p className="text-[11px] text-zinc-500">Suspicion</p>
        <p className={cn("mt-1 font-mono text-xl font-medium tabular-nums", suspBand.textClass)}>
          {formatScore(suspicion)}
        </p>
        <p className="mt-0.5 text-[11px] text-zinc-500">{suspBand.label}</p>
      </div>

      <div className="sm:px-4">
        <p className="text-[11px] text-zinc-500">Engagement</p>
        <p className="mt-1 font-mono text-xl font-medium tabular-nums text-zinc-100">
          {formatScore(engagement)}
        </p>
        {mood && <p className="mt-0.5 truncate text-[11px] text-zinc-500">{mood}</p>}
      </div>

      <div className="col-span-2 sm:col-span-1 sm:pl-4">
        <p className="text-[11px] text-zinc-500">Profile Confidence</p>
        <p className="mt-1 font-mono text-xl font-medium tabular-nums text-zinc-100">
          {formatPercent(profileConfidence)}
        </p>
      </div>
    </div>
  );
}
