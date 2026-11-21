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
    <Card className="border-zinc-800/80">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-zinc-100">Suspicion Watchouts</CardTitle>
      </CardHeader>
      <CardContent>
        {activeWatchouts.length === 0 ? (
          <p className="text-xs text-zinc-500">None observed up to v{selectedVersion}.</p>
        ) : (
          <ul className="divide-y divide-zinc-800/70">
            {activeWatchouts.map((text) => {
              const meta = byText.get(text);
              return (
                <li key={text} className="flex items-center justify-between gap-3 py-2 text-xs">
                  <span className="text-zinc-300">{text}</span>
                  {meta && (
                    <button
                      type="button"
                      onClick={() => onSelectVersion?.(meta.first_seen_version)}
                      className="shrink-0 font-mono text-[11px] text-zinc-500 hover:text-zinc-200 cursor-pointer"
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
