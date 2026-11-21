"use client";

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
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { SignalPoint, TimelineMarker } from "@/lib/types";
import { cn, formatScore, getSuspicionBand } from "@/lib/utils";

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

export function SuspicionChart({
  signals,
  markers,
  selectedVersion,
  onSelectVersion,
}: SuspicionChartProps) {
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

  return (
    <Card className="border-zinc-800/80">
      <CardHeader className="flex-row items-baseline justify-between space-y-0 pb-4">
        <div>
          <CardTitle className="text-sm font-medium text-zinc-100">
            Suspicion &amp; Engagement
          </CardTitle>
          <CardDescription className="mt-0.5 text-xs text-zinc-500">
            Rose: suspicion · Light: engagement · Dashed: 0.60 pivot line
          </CardDescription>
        </div>
        {latest && (
          <span className={cn("font-mono text-xs tabular-nums", suspBand.textClass)}>
            {formatScore(latest.suspicion)} {suspBand.label}
          </span>
        )}
      </CardHeader>

      <CardContent className="space-y-4">
        {chartData.length === 0 ? (
          <div className="flex h-52 items-center justify-center rounded-lg border border-dashed border-zinc-800 text-xs text-zinc-500">
            No reply signals recorded up to v{selectedVersion}.
          </div>
        ) : (
          <div className="h-56 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart
                data={chartData}
                margin={{ top: 8, right: 24, left: -20, bottom: 0 }}
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
                    <stop offset="5%" stopColor="#F43F5E" stopOpacity={0.16} />
                    <stop offset="95%" stopColor="#F43F5E" stopOpacity={0.0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#27272A" strokeDasharray="2 4" vertical={false} />
                <XAxis
                  dataKey="label"
                  tick={{ fill: "#71717A", fontSize: 11, fontFamily: "var(--font-mono)" }}
                  axisLine={{ stroke: "#27272A" }}
                  tickLine={false}
                />
                <YAxis
                  domain={[0, 1]}
                  ticks={[0, 0.3, 0.6, 1.0]}
                  tick={{ fill: "#71717A", fontSize: 11, fontFamily: "var(--font-mono)" }}
                  axisLine={false}
                  tickLine={false}
                />
                <ReferenceLine
                  y={0.6}
                  stroke="#52525B"
                  strokeDasharray="4 4"
                  label={{
                    value: "0.60 pivot",
                    position: "insideTopRight",
                    fill: "#71717A",
                    fontSize: 10,
                    fontFamily: "var(--font-mono)",
                  }}
                />
                <Tooltip
                  content={({ active, payload }) => {
                    if (!active || !payload?.length) return null;
                    const d = payload[0].payload as ChartDatum;
                    return (
                      <div className="max-w-xs rounded-lg border border-zinc-800 bg-[#141417] p-3 text-xs shadow-lg">
                        <div className="flex items-center justify-between gap-4 font-mono text-[11px] text-zinc-400">
                          <span>
                            {d.label} · v{d.version}
                          </span>
                          <span>{d.channel}</span>
                        </div>
                        <div className="mt-1.5 flex items-center gap-4 font-mono text-xs tabular-nums">
                          <span className="text-rose-400">Suspicion {d.suspicion.toFixed(2)}</span>
                          <span className="text-zinc-200">Engagement {d.engagement.toFixed(2)}</span>
                        </div>
                        {d.reason && <p className="mt-1.5 text-[11px] text-zinc-400">{d.reason}</p>}
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
                  dot={{ r: 3, fill: "#F43F5E", stroke: "#09090B", strokeWidth: 1.5 }}
                  activeDot={{ r: 4.5 }}
                />
                <Line
                  type="monotone"
                  dataKey="engagement"
                  stroke="#D4D4D8"
                  strokeWidth={1.75}
                  dot={{ r: 2.5, fill: "#D4D4D8", stroke: "#09090B", strokeWidth: 1.5 }}
                  activeDot={{ r: 4 }}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        )}

        {latest && (
          <div className="border-t border-zinc-800/80 pt-3 text-xs">
            <div className="flex items-center justify-between text-zinc-400">
              <span>
                Mood: <span className="text-zinc-200">{latest.mood || "neutral"}</span> · Next:{" "}
                <span className="font-mono text-zinc-200">{latest.proposed_next}</span>
              </span>
              <span className="font-mono text-[11px] text-zinc-500">v{latest.version}</span>
            </div>
            {latest.reason && <p className="mt-1 text-zinc-400">{latest.reason}</p>}
          </div>
        )}

        {visibleMarkers.length > 0 && (
          <div className="space-y-1 border-t border-zinc-800/80 pt-3">
            {visibleMarkers.map((m, idx) => (
              <button
                key={`${m.kind}-${m.version}-${idx}`}
                type="button"
                onClick={() => onSelectVersion?.(m.version)}
                className="flex w-full items-baseline justify-between gap-2 rounded px-1.5 py-1 text-left text-xs text-zinc-400 transition-colors hover:bg-zinc-900 hover:text-zinc-200 cursor-pointer"
              >
                <span className="truncate">{m.label}</span>
                <span className="shrink-0 font-mono text-[10px] text-zinc-500">v{m.version}</span>
              </button>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
