"use client";

import { useState } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { SignalPoint, TimelineMarker } from "@/lib/types";
import {
  cn,
  formatChannel,
  formatMarkerLabel,
  formatProposedNext,
  formatScore,
  getSuspicionBand,
} from "@/lib/utils";

interface SuspicionChartProps {
  signals: SignalPoint[];
  markers: TimelineMarker[];
  selectedVersion: number;
  onSelectVersion?: (version: number) => void;
}

interface ChartDatum {
  label: string;
  touch_n: number;
  version: number;
  suspicion: number;
  engagement: number;
  mood: string;
  proposed_next: string;
  reason: string;
  channel: string;
}

const MAX_DEFAULT_MARKERS = 3;

export function SuspicionChart({
  signals,
  markers,
  selectedVersion,
  onSelectVersion,
}: SuspicionChartProps) {
  const [showAllMarkers, setShowAllMarkers] = useState(false);

  const visibleSignals = signals.filter((s) => s.version <= selectedVersion);
  let sessionIdx = 1;
  let prevTouch = 0;
  const chartData: ChartDatum[] = visibleSignals.map((s, idx) => {
    const t = s.touch_n || idx + 1;
    if (idx > 0 && t <= prevTouch) {
      sessionIdx += 1;
    }
    prevTouch = t;
    return {
      label: sessionIdx > 1 ? `S${sessionIdx}·T${t}` : `T${t}`,
      touch_n: s.touch_n,
      version: s.version,
      suspicion: Number(s.suspicion.toFixed(2)),
      engagement: Number(s.engagement.toFixed(2)),
      mood: s.mood,
      proposed_next: s.proposed_next,
      reason: s.reason,
      channel: s.channel,
    };
  });

  const latest = visibleSignals[visibleSignals.length - 1] ?? null;
  const suspBand = getSuspicionBand(latest?.suspicion ?? null);
  const visibleMarkers = markers.filter((m) => m.version <= selectedVersion);
  const displayedMarkers =
    showAllMarkers || visibleMarkers.length <= MAX_DEFAULT_MARKERS
      ? visibleMarkers
      : visibleMarkers.slice(-MAX_DEFAULT_MARKERS);
  const hiddenMarkerCount = visibleMarkers.length - displayedMarkers.length;

  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-start justify-between gap-2 space-y-0 pb-4">
        <div className="min-w-0 space-y-1.5">
          <CardTitle>Suspicion &amp; Engagement</CardTitle>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-zinc-400">
            <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
              <span className="h-[2px] w-3 rounded-full bg-rose-500" />
              Suspicion
            </span>
            <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
              <span className="h-[2px] w-3 rounded-full bg-zinc-300" />
              Engagement
            </span>
            <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
              <span className="w-3 border-t border-dashed border-zinc-500" />
              0.60 pivot
            </span>
          </div>
        </div>
        {latest && (
          <div className="shrink-0 text-right">
            <span className={cn("font-mono text-sm font-medium tabular-nums", suspBand.textClass)}>
              {formatScore(latest.suspicion)}
            </span>
            <span className="ml-1.5 text-xs text-zinc-400">{suspBand.label}</span>
          </div>
        )}
      </CardHeader>

      <CardContent className="space-y-4">
        {chartData.length === 0 ? (
          <div className="flex h-52 items-center justify-center rounded-lg border border-dashed border-zinc-800 text-xs text-zinc-400">
            No target reply signals recorded up to v{selectedVersion}.
          </div>
        ) : (
          <div className="h-56 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart
                data={chartData}
                margin={{ top: 12, right: 16, left: -12, bottom: 0 }}
                onClick={(state) => {
                  const activePayload = state?.activePayload?.[0]?.payload as
                    | ChartDatum
                    | undefined;
                  if (activePayload && onSelectVersion) {
                    onSelectVersion(activePayload.version);
                  }
                }}
              >
                <defs>
                  <linearGradient id="suspicionFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#F43F5E" stopOpacity={0.14} />
                    <stop offset="95%" stopColor="#F43F5E" stopOpacity={0.0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#27272A" strokeDasharray="2 4" vertical={false} />
                <XAxis
                  dataKey="label"
                  minTickGap={22}
                  tick={{ fill: "#A1A1AA", fontSize: 11, fontFamily: "var(--font-mono)" }}
                  axisLine={{ stroke: "#27272A" }}
                  tickLine={false}
                />
                <YAxis
                  domain={[0, 1]}
                  ticks={[0, 0.3, 0.6, 1.0]}
                  tick={{ fill: "#A1A1AA", fontSize: 11, fontFamily: "var(--font-mono)" }}
                  axisLine={false}
                  tickLine={false}
                />
                <ReferenceLine y={0.6} stroke="#52525B" strokeDasharray="4 4" />
                <Tooltip
                  content={({ active, payload }) => {
                    if (!active || !payload?.length) return null;
                    const d = payload[0].payload as ChartDatum;
                    return (
                      <div className="max-w-xs rounded-lg border border-zinc-700/80 bg-card-elevated p-3 text-xs shadow-xl">
                        <div className="flex items-center justify-between gap-4 text-zinc-400">
                          <span className="font-mono tabular-nums">
                            {d.label} · v{d.version}
                          </span>
                          <span>{formatChannel(d.channel)}</span>
                        </div>
                        <div className="mt-2 flex items-center gap-4 font-mono tabular-nums">
                          <span className="text-rose-400">
                            Suspicion {d.suspicion.toFixed(2)}
                          </span>
                          <span className="text-zinc-200">
                            Engagement {d.engagement.toFixed(2)}
                          </span>
                        </div>
                        {d.proposed_next && (
                          <p className="mt-1.5 text-zinc-300">
                            Next move: {formatProposedNext(d.proposed_next)}
                          </p>
                        )}
                        {d.reason && (
                          <p className="mt-1 leading-relaxed text-zinc-400">{d.reason}</p>
                        )}
                      </div>
                    );
                  }}
                />
                <Area
                  type="monotone"
                  dataKey="suspicion"
                  stroke="#F43F5E"
                  strokeWidth={2}
                  fill="url(#suspicionFill)"
                  dot={{ r: 3, fill: "#F43F5E", stroke: "#111114", strokeWidth: 1.5 }}
                  activeDot={{ r: 4.5, cursor: "pointer" }}
                />
                <Line
                  type="monotone"
                  dataKey="engagement"
                  stroke="#D4D4D8"
                  strokeWidth={1.75}
                  dot={{ r: 2.5, fill: "#D4D4D8", stroke: "#111114", strokeWidth: 1.5 }}
                  activeDot={{ r: 4, cursor: "pointer" }}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        )}

        {latest && (
          <div className="space-y-1.5 border-t border-zinc-800/80 pt-3.5 text-xs">
            <div className="flex items-baseline justify-between gap-3">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-zinc-400">
                <span>
                  Mood: <span className="text-zinc-200">{latest.mood || "Neutral"}</span>
                </span>
                {latest.proposed_next && (
                  <span>
                    Next move:{" "}
                    <span className="text-zinc-200">
                      {formatProposedNext(latest.proposed_next)}
                    </span>
                  </span>
                )}
              </div>
              <span className="shrink-0 font-mono tabular-nums text-zinc-400">
                v{latest.version}
              </span>
            </div>
            {latest.reason && (
              <p className="leading-relaxed text-zinc-300">{latest.reason}</p>
            )}
          </div>
        )}

        {visibleMarkers.length > 0 && (
          <div className="space-y-1 border-t border-zinc-800/80 pt-3">
            <div className="mb-1.5 flex items-center justify-between text-xs">
              <span className="text-zinc-400">Strategy &amp; campaign milestones</span>
              {visibleMarkers.length > MAX_DEFAULT_MARKERS && (
                <button
                  type="button"
                  onClick={() => setShowAllMarkers((prev) => !prev)}
                  className="cursor-pointer text-zinc-400 transition-colors hover:text-zinc-100"
                >
                  {showAllMarkers
                    ? "Show recent"
                    : `Show all ${visibleMarkers.length} (${hiddenMarkerCount} earlier)`}
                </button>
              )}
            </div>
            {displayedMarkers.map((m, idx) => {
              const formatted = formatMarkerLabel(m.label);
              return (
                <button
                  key={`${m.kind}-${m.version}-${idx}`}
                  type="button"
                  onClick={() => onSelectVersion?.(m.version)}
                  className="group flex w-full cursor-pointer items-baseline justify-between gap-3 rounded-md px-2 py-1.5 text-left text-xs transition-colors hover:bg-card-elevated/70"
                >
                  <div className="min-w-0">
                    <p className="text-zinc-200 group-hover:text-zinc-50">{formatted.title}</p>
                    {formatted.detail && (
                      <p className="mt-0.5 line-clamp-1 text-zinc-400">{formatted.detail}</p>
                    )}
                  </div>
                  <span className="shrink-0 font-mono text-[11px] tabular-nums text-zinc-400 group-hover:text-zinc-200">
                    v{m.version}
                  </span>
                </button>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
