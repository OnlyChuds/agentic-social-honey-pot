"use client";

import { useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import type { FactJSON, PersonalityMilestone } from "@/lib/types";
import { cn, formatPercent, formatScore, formatTraitLabel } from "@/lib/utils";

interface PersonalityEvolutionPanelProps {
  initialMilestone: PersonalityMilestone | null;
  currentMilestone: PersonalityMilestone | null;
  factsById: Record<string, FactJSON>;
}

export function PersonalityEvolutionPanel({
  initialMilestone,
  currentMilestone,
  factsById,
}: PersonalityEvolutionPanelProps) {
  const [expandedTrait, setExpandedTrait] = useState<string | null>(null);
  const [showSeedDiff, setShowSeedDiff] = useState(false);

  if (!currentMilestone) {
    return (
      <Card className="border-zinc-800/80">
        <CardHeader>
          <CardTitle className="text-sm font-medium text-zinc-100">Personality Evolution</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-zinc-500">No profile milestones recorded yet.</p>
        </CardContent>
      </Card>
    );
  }

  const profile = currentMilestone.profile;
  const initProfile = initialMilestone?.profile ?? null;
  const hasStyleDiff =
    initProfile !== null &&
    ((Boolean(initProfile.style_guide) && initProfile.style_guide !== profile.style_guide) ||
      (Boolean(initProfile.trust_style) && initProfile.trust_style !== profile.trust_style));

  return (
    <Card className="border-zinc-800/80">
      <CardHeader className="flex-row items-baseline justify-between space-y-0 pb-4">
        <div>
          <CardTitle className="text-sm font-medium text-zinc-100">Personality Evolution</CardTitle>
          <CardDescription className="mt-0.5 text-xs text-zinc-500">
            Traits &amp; calibration at v{currentMilestone.version} · click any trait for evidence
          </CardDescription>
        </div>
        <span className="font-mono text-xs tabular-nums text-zinc-400">
          {formatPercent(profile.overall_confidence)} conf
        </span>
      </CardHeader>

      <CardContent className="space-y-6">
        {/* Psych Traits */}
        <div className="space-y-3">
          {currentMilestone.traits.length === 0 ? (
            <p className="text-xs text-zinc-500">Awaiting profile traits...</p>
          ) : (
            currentMilestone.traits.map((t) => {
              const isOpen = expandedTrait === t.trait;
              const deltaPct = Math.round(t.delta * 100);

              return (
                <div key={t.trait} className="space-y-1.5">
                  <button
                    type="button"
                    onClick={() => setExpandedTrait(isOpen ? null : t.trait)}
                    className="flex w-full items-baseline justify-between gap-2 text-left cursor-pointer group"
                  >
                    <span className="text-xs font-medium text-zinc-200 group-hover:text-zinc-50">
                      {formatTraitLabel(t.trait)}
                    </span>
                    <span className="flex shrink-0 items-center gap-2 font-mono text-[11px] tabular-nums">
                      {t.is_new ? (
                        <span className="text-emerald-400">new</span>
                      ) : Math.abs(deltaPct) >= 1 ? (
                        <span className={cn(deltaPct > 0 ? "text-emerald-400" : "text-rose-400")}>
                          {deltaPct > 0 ? `+${deltaPct}%` : `${deltaPct}%`}
                        </span>
                      ) : null}
                      <span className="text-zinc-300">{formatPercent(t.confidence)}</span>
                    </span>
                  </button>

                  <Progress
                    value={t.confidence * 100}
                    className="h-1 bg-zinc-800/90"
                    indicatorClassName="bg-zinc-200"
                  />

                  {isOpen && t.evidence.length > 0 && (
                    <div className="mt-2 space-y-1.5 rounded-lg bg-zinc-900/70 p-2.5 text-[11px]">
                      {t.evidence.map((eid) => {
                        const f = factsById[eid];
                        return (
                          <div key={eid} className="text-zinc-400">
                            <span className="font-mono text-zinc-300">
                              {eid}
                              {f?.osint_grade ? ` (${f.osint_grade})` : ""}
                            </span>
                            {f ? ` — ${f.claim}` : " — observed in conversation"}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>

        {/* Style & Trust */}
        <div className="space-y-3 border-t border-zinc-800/80 pt-4">
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-medium text-zinc-300">Communication &amp; Trust</h3>
            {hasStyleDiff && (
              <button
                type="button"
                onClick={() => setShowSeedDiff((v) => !v)}
                className="text-[11px] text-zinc-500 hover:text-zinc-200 cursor-pointer"
              >
                {showSeedDiff ? "Hide initial" : "Compare to initial"}
              </button>
            )}
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <p className="text-[11px] text-zinc-500">Style</p>
              <p className="mt-1 text-xs leading-relaxed text-zinc-300">
                {profile.style_guide || "Not yet observed"}
              </p>
              {showSeedDiff && initProfile?.style_guide && (
                <p className="mt-1.5 text-[11px] text-zinc-500">
                  Initial: {initProfile.style_guide}
                </p>
              )}
            </div>

            <div>
              <p className="text-[11px] text-zinc-500">Trust Pattern</p>
              <p className="mt-1 text-xs leading-relaxed text-zinc-300">
                {profile.trust_style || "Not yet observed"}
              </p>
              {showSeedDiff && initProfile?.trust_style && (
                <p className="mt-1.5 text-[11px] text-zinc-500">
                  Initial: {initProfile.trust_style}
                </p>
              )}
            </div>
          </div>
        </div>

        {/* Stressors & Hooks */}
        {(profile.stressors.length > 0 || currentMilestone.hooks.length > 0) && (
          <div className="grid grid-cols-1 gap-4 border-t border-zinc-800/80 pt-4 sm:grid-cols-2">
            <div>
              <p className="text-[11px] text-zinc-500">Stressors</p>
              <ul className="mt-2 space-y-1.5">
                {profile.stressors.map((s) => (
                  <li key={s.topic} className="flex items-baseline justify-between gap-2 text-xs">
                    <span className="truncate text-zinc-300">{s.topic}</span>
                    <span className="shrink-0 font-mono text-[11px] text-zinc-500">
                      {formatPercent(s.confidence)}
                    </span>
                  </li>
                ))}
              </ul>
            </div>

            <div>
              <p className="text-[11px] text-zinc-500">Active Hooks</p>
              <ul className="mt-2 space-y-1.5">
                {currentMilestone.hooks.map((h) => (
                  <li key={h.hook_id} className="flex items-baseline justify-between gap-2 text-xs">
                    <span
                      className={cn(
                        "truncate",
                        h.status === "exhausted" ? "text-zinc-500 line-through" : "text-zinc-300"
                      )}
                    >
                      {h.topic}
                    </span>
                    <span className="shrink-0 font-mono text-[11px] text-zinc-500">
                      {formatScore(h.engagement.score)}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
