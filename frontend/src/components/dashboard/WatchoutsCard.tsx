"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { WatchoutEntry } from "@/lib/types";

interface WatchoutsCardProps {
  watchouts: WatchoutEntry[];
  activeWatchouts: string[];
  selectedVersion: number;
  onSelectVersion?: (version: number) => void;
}

export function WatchoutsCard({
  watchouts,
  activeWatchouts,
  selectedVersion,
  onSelectVersion,
}: WatchoutsCardProps) {
  const byText = new Map(watchouts.map((w) => [w.text, w]));

  return (
    <Card>
      <CardHeader className="flex-row items-baseline justify-between space-y-0 pb-2.5">
        <CardTitle>Suspicion Watchouts</CardTitle>
        {activeWatchouts.length > 0 && (
          <span className="text-xs text-zinc-400">Click version to inspect origin</span>
        )}
      </CardHeader>
      <CardContent>
        {activeWatchouts.length === 0 ? (
          <p className="text-xs text-zinc-400">No watchouts observed up to v{selectedVersion}.</p>
        ) : (
          <ul className="divide-y divide-zinc-800/70">
            {activeWatchouts.map((text) => {
              const meta = byText.get(text);
              return (
                <li
                  key={text}
                  className="flex items-baseline justify-between gap-4 py-2.5 text-[13px] leading-snug"
                >
                  <span className="text-zinc-200">{text}</span>
                  {meta && (
                    <button
                      type="button"
                      onClick={() => onSelectVersion?.(meta.first_seen_version)}
                      title={`Jump timeline to v${meta.first_seen_version}`}
                      className="shrink-0 cursor-pointer font-mono text-xs tabular-nums text-zinc-400 transition-colors hover:text-zinc-100"
                    >
                      v{meta.first_seen_version}
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
