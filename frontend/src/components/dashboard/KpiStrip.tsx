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
    <div className="grid grid-cols-5 divide-x divide-zinc-800/80 rounded-xl border border-zinc-800/80 bg-card px-4 py-4">
      <div className="min-w-0 pr-4">
        <p className="truncate text-xs text-zinc-400">Touches</p>
        <p className="mt-1.5 font-mono text-2xl font-semibold tabular-nums text-zinc-50">
          {touchesUsed}
          <span className="text-base font-normal text-zinc-500">/{sessionMax}</span>
        </p>
        {incidentsCount > 0 && (
          <p className="mt-1 text-xs text-emerald-400">Objective met</p>
        )}
      </div>

      <div className="min-w-0 px-4">
        <p className="truncate text-xs text-zinc-400">Sessions</p>
        <p className="mt-1.5 font-mono text-2xl font-semibold tabular-nums text-zinc-50">
          {sessionsUsed}
          <span className="text-base font-normal text-zinc-500">/{sessionsAllowed}</span>
        </p>
        {sessionsUsed > 1 && <p className="mt-1 truncate text-xs text-zinc-400">Memory retained</p>}
      </div>

      <div className="min-w-0 px-4">
        <p className="truncate text-xs text-zinc-400">Suspicion</p>
        <p
          className={cn(
            "mt-1.5 font-mono text-2xl font-semibold tabular-nums",
            suspicion === null ? "text-zinc-500" : suspBand.textClass
          )}
        >
          {formatScore(suspicion)}
        </p>
        <p
          className={cn(
            "mt-1 truncate text-xs",
            suspicion !== null && suspicion >= 0.6 ? "text-rose-400/90" : "text-zinc-400"
          )}
        >
          {suspBand.label}
        </p>
      </div>

      <div className="min-w-0 px-4">
        <p className="truncate text-xs text-zinc-400">Engagement</p>
        <p
          className={cn(
            "mt-1.5 font-mono text-2xl font-semibold tabular-nums",
            engagement === null ? "text-zinc-500" : "text-zinc-50"
          )}
        >
          {formatScore(engagement)}
        </p>
        {mood && (
          <p className="mt-1 truncate text-xs text-zinc-400" title={mood}>
            {mood.split(";")[0].trim()}
          </p>
        )}
      </div>

      <div className="min-w-0 pl-4">
        <p className="truncate text-xs text-zinc-400">Profile confidence</p>
        <p className="mt-1.5 font-mono text-2xl font-semibold tabular-nums text-zinc-50">
          {formatPercent(profileConfidence)}
        </p>
      </div>
    </div>
  );
}
