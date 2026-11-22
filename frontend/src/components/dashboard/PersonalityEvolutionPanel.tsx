"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import { useMemo, useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import type { EventJSON, FactJSON, PersonalityMilestone } from "@/lib/types";
import {
  cn,
  formatChannel,
  formatDomainFromUrl,
  formatPercent,
  formatScore,
  formatTraitLabel,
} from "@/lib/utils";

interface PersonalityEvolutionPanelProps {
  initialMilestone: PersonalityMilestone | null;
  currentMilestone: PersonalityMilestone | null;
  factsById: Record<string, FactJSON>;
  events?: EventJSON[];
  onSelectVersion?: (version: number) => void;
}

function summarizeEventEvidence(ev: EventJSON): string {
  const p = ev.payload ?? {};
  const candidates = [p.reason, p.body, p.text, p.summary, p.subject];
  for (const val of candidates) {
    if (typeof val === "string" && val.trim()) {
      const plain = val.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
      if (plain) {
        return plain.length > 160 ? `${plain.slice(0, 157)}…` : plain;
      }
    }
  }
  const channel = typeof p.channel === "string" ? formatChannel(p.channel) : null;
  if (channel) return `Observed in ${channel.toLowerCase()} exchange`;
  return "Observed during conversation";
}

export function PersonalityEvolutionPanel({
  initialMilestone,
  currentMilestone,
  factsById,
  events = [],
  onSelectVersion,
}: PersonalityEvolutionPanelProps) {
  const [expandedTrait, setExpandedTrait] = useState<string | null>(null);
  const [expandedHook, setExpandedHook] = useState<string | null>(null);
  const [showSeedDiff, setShowSeedDiff] = useState(false);

  const eventsById = useMemo(
    () => new Map(events.map((e) => [e.event_id, e])),
    [events]
  );

  if (!currentMilestone) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Personality Evolution</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-zinc-400">No profile milestones recorded yet.</p>
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
    <Card>
      <CardHeader className="flex-row items-baseline justify-between space-y-0 pb-4">
        <div>
          <CardTitle>Personality Evolution</CardTitle>
          <CardDescription className="mt-1">
            Modelled traits at v{currentMilestone.version} · expand any trait to inspect supporting
            evidence
          </CardDescription>
        </div>
        <span className="shrink-0 font-mono text-xs tabular-nums text-zinc-300">
          {formatPercent(profile.overall_confidence)}{" "}
          <span className="font-sans text-zinc-400">confidence</span>
        </span>
      </CardHeader>

      <CardContent className="space-y-6">
        {/* Psych Traits */}
        <div className="space-y-3.5">
          {currentMilestone.traits.length === 0 ? (
            <p className="text-xs text-zinc-400">No traits inferred at this snapshot.</p>
          ) : (
            currentMilestone.traits.map((t) => {
              const hasEvidence = t.evidence.length > 0;
              const isOpen = hasEvidence && expandedTrait === t.trait;
              const deltaPct = Math.round(t.delta * 100);

              return (
                <div key={t.trait} className="space-y-1.5">
                  {hasEvidence ? (
                    <button
                      type="button"
                      onClick={() => setExpandedTrait(isOpen ? null : t.trait)}
                      className="group flex w-full cursor-pointer items-start justify-between gap-3 text-left"
                    >
                      <span className="flex items-start gap-1.5 text-[13px] leading-snug text-zinc-200 group-hover:text-zinc-50">
                        {isOpen ? (
                          <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 text-zinc-400" />
                        ) : (
                          <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-zinc-500 group-hover:text-zinc-300" />
                        )}
                        <span>{formatTraitLabel(t.trait)}</span>
                      </span>
                      <span className="flex shrink-0 items-baseline gap-2 font-mono text-xs tabular-nums">
                        {t.is_new ? (
                          <span className="font-sans text-[11px] font-medium text-emerald-400">
                            New
                          </span>
                        ) : Math.abs(deltaPct) >= 1 ? (
                          <span className={cn(deltaPct > 0 ? "text-emerald-400" : "text-rose-400")}>
                            {deltaPct > 0 ? `+${deltaPct}%` : `${deltaPct}%`}
                          </span>
                        ) : null}
                        <span className="text-zinc-200">{formatPercent(t.confidence)}</span>
                      </span>
                    </button>
                  ) : (
                    <div className="flex w-full items-start justify-between gap-3">
                      <span className="pl-5 text-[13px] leading-snug text-zinc-200">
                        {formatTraitLabel(t.trait)}
                      </span>
                      <span className="flex shrink-0 items-baseline gap-2 font-mono text-xs tabular-nums">
                        {t.is_new ? (
                          <span className="font-sans text-[11px] font-medium text-emerald-400">
                            New
                          </span>
                        ) : Math.abs(deltaPct) >= 1 ? (
                          <span className={cn(deltaPct > 0 ? "text-emerald-400" : "text-rose-400")}>
                            {deltaPct > 0 ? `+${deltaPct}%` : `${deltaPct}%`}
                          </span>
                        ) : null}
                        <span className="text-zinc-200">{formatPercent(t.confidence)}</span>
                      </span>
                    </div>
                  )}

                  <div className="pl-5">
                    <Progress
                      value={t.confidence * 100}
                      className="h-1 bg-zinc-800"
                      indicatorClassName="bg-zinc-200"
                    />
                  </div>

                  {isOpen && (
                    <div className="ml-5 mt-2 space-y-2 border-l border-zinc-800 pl-3 text-xs">
                      {t.evidence.map((eid) => {
                        const fact = factsById[eid];
                        const ev = eventsById.get(eid);

                        if (fact) {
                          const domain = formatDomainFromUrl(fact.source);
                          return (
                            <div key={eid} className="space-y-0.5 text-zinc-300">
                              <p className="leading-relaxed">{fact.claim}</p>
                              <p className="text-[11px] text-zinc-400">
                                {fact.provenance === "osint"
                                  ? `OSINT${fact.osint_grade ? ` · Grade ${fact.osint_grade}` : ""}`
                                  : fact.provenance === "target_claim"
                                    ? "Target statement"
                                    : "Inferred"}
                                {domain && (
                                  <>
                                    {" · "}
                                    <a
                                      href={fact.source}
                                      target="_blank"
                                      rel="noreferrer"
                                      className="underline decoration-zinc-600 underline-offset-2 hover:text-zinc-200"
                                    >
                                      {domain}
                                    </a>
                                  </>
                                )}
                              </p>
                            </div>
                          );
                        }

                        if (ev) {
                          return (
                            <div
                              key={eid}
                              className="flex items-baseline justify-between gap-3 text-zinc-300"
                            >
                              <p className="leading-relaxed">{summarizeEventEvidence(ev)}</p>
                              {ev.base_version > 0 && onSelectVersion && (
                                <button
                                  type="button"
                                  onClick={() => onSelectVersion(ev.base_version)}
                                  className="shrink-0 cursor-pointer font-mono text-[11px] tabular-nums text-zinc-400 hover:text-zinc-100"
                                >
                                  v{ev.base_version}
                                </button>
                              )}
                            </div>
                          );
                        }

                        return (
                          <p key={eid} className="text-zinc-400">
                            Observed during conversation
                          </p>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>

        {/* Communication & Trust */}
        <div className="space-y-3 border-t border-zinc-800/80 pt-5">
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-medium text-zinc-200">Communication &amp; Trust</h3>
            {hasStyleDiff && (
              <button
                type="button"
                onClick={() => setShowSeedDiff((v) => !v)}
                className="cursor-pointer text-xs text-zinc-400 transition-colors hover:text-zinc-100"
              >
                {showSeedDiff ? "Hide initial baseline" : "Compare to initial"}
              </button>
            )}
          </div>

          <div className="grid grid-cols-2 gap-5">
            <div>
              <p className="text-xs text-zinc-400">Style</p>
              <p className="mt-1 text-[13px] leading-relaxed text-zinc-200">
                {profile.style_guide || "Not yet observed"}
              </p>
              {showSeedDiff && initProfile?.style_guide && (
                <p className="mt-2 border-l border-zinc-700 pl-2.5 text-xs leading-relaxed text-zinc-400">
                  Initial: {initProfile.style_guide}
                </p>
              )}
            </div>

            <div>
              <p className="text-xs text-zinc-400">Trust pattern</p>
              <p className="mt-1 text-[13px] leading-relaxed text-zinc-200">
                {profile.trust_style || "Not yet observed"}
              </p>
              {showSeedDiff && initProfile?.trust_style && (
                <p className="mt-2 border-l border-zinc-700 pl-2.5 text-xs leading-relaxed text-zinc-400">
                  Initial: {initProfile.trust_style}
                </p>
              )}
            </div>
          </div>
        </div>

        {/* Stressors & Hooks (stacked vertically so full text reads cleanly without truncation) */}
        {(profile.stressors.length > 0 || currentMilestone.hooks.length > 0) && (
          <div className="space-y-5 border-t border-zinc-800/80 pt-5">
            <div>
              <p className="text-xs font-medium text-zinc-300">Stressors</p>
              {profile.stressors.length === 0 ? (
                <p className="mt-1.5 text-xs text-zinc-400">No stressors identified yet.</p>
              ) : (
                <ul className="mt-2 space-y-2">
                  {profile.stressors.map((s) => (
                    <li
                      key={s.topic}
                      className="flex items-baseline justify-between gap-4 text-[13px] leading-snug"
                    >
                      <span className="text-zinc-200">{s.topic}</span>
                      <span className="shrink-0 font-mono text-xs tabular-nums text-zinc-400">
                        {formatPercent(s.confidence)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div className="border-t border-zinc-800/60 pt-4">
              <p className="text-xs font-medium text-zinc-300">Hooks</p>
              {currentMilestone.hooks.length === 0 ? (
                <p className="mt-1.5 text-xs text-zinc-400">No hooks ranked yet.</p>
              ) : (
                <ul className="mt-2 space-y-2">
                  {currentMilestone.hooks.map((h) => {
                    const hasDetail = Boolean(h.why_leverage);
                    const isOpen = expandedHook === h.hook_id;

                    return (
                      <li key={h.hook_id} className="text-[13px] leading-snug">
                        <div className="flex items-baseline justify-between gap-4">
                          {hasDetail ? (
                            <button
                              type="button"
                              onClick={() => setExpandedHook(isOpen ? null : h.hook_id)}
                              className="group flex cursor-pointer items-start gap-1.5 text-left"
                            >
                              {isOpen ? (
                                <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 text-zinc-400" />
                              ) : (
                                <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-zinc-500 group-hover:text-zinc-300" />
                              )}
                              <span
                                className={cn(
                                  h.status === "exhausted"
                                    ? "text-zinc-500 line-through"
                                    : "text-zinc-200 group-hover:text-zinc-50"
                                )}
                              >
                                {h.topic}
                              </span>
                            </button>
                          ) : (
                            <span
                              className={cn(
                                "pl-5",
                                h.status === "exhausted"
                                  ? "text-zinc-500 line-through"
                                  : "text-zinc-200"
                              )}
                            >
                              {h.topic}
                            </span>
                          )}
                          <span className="shrink-0 font-mono text-xs tabular-nums text-zinc-400">
                            {formatScore(h.engagement.score)}
                          </span>
                        </div>
                        {isOpen && h.why_leverage && (
                          <p className="ml-5 mt-1.5 border-l border-zinc-800 pl-3 text-xs leading-relaxed text-zinc-400">
                            {h.why_leverage}
                            {h.engagement.touches_touched > 0 && (
                              <span className="ml-2 text-zinc-500">
                                ({h.engagement.positive}/{h.engagement.touches_touched} positive
                                touches)
                              </span>
                            )}
                          </p>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
