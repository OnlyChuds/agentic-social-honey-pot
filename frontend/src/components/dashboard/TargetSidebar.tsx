"use client";

import { Play, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { LaunchPracticeDialog } from "@/components/dashboard/LaunchPracticeDialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { createOfflineReplay } from "@/lib/api";
import type { LaunchRunResponse, TargetSummary } from "@/lib/types";
import { cn, formatScore, getStageTextClass, getSuspicionBand } from "@/lib/utils";

interface TargetSidebarProps {
  targets: TargetSummary[];
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
  onRefresh: () => void;
  onLaunched: (res: LaunchRunResponse) => void;
  offlineReplaysAvailable: boolean | null;
  onReplayCreated: (runId: string) => void;
  isConnected: boolean;
  isLoading: boolean;
}

export function TargetSidebar({
  targets,
  selectedRunId,
  onSelect,
  onRefresh,
  onLaunched,
  offlineReplaysAvailable,
  onReplayCreated,
  isConnected,
  isLoading,
}: TargetSidebarProps) {
  const [search, setSearch] = useState("");
  const [creatingReplay, setCreatingReplay] = useState(false);
  const [replayError, setReplayError] = useState<string | null>(null);

  async function handleCreateReplay() {
    setCreatingReplay(true);
    setReplayError(null);
    try {
      const { run_id } = await createOfflineReplay();
      onReplayCreated(run_id);
    } catch (err) {
      setReplayError(err instanceof Error ? err.message : "Could not create offline replay.");
    } finally {
      setCreatingReplay(false);
    }
  }

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return targets
      .filter((t) => {
        if (!q) return true;
        return (
          t.target_name.toLowerCase().includes(q) ||
          t.target_id.toLowerCase().includes(q) ||
          t.target_org.toLowerCase().includes(q)
        );
      })
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  }, [targets, search]);

  return (
    <aside className="flex h-full w-full flex-col border-r border-zinc-800/80 bg-[#0B0B0E] lg:w-72 lg:shrink-0">
      <div className="space-y-3 border-b border-zinc-800/80 p-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-sm font-semibold tracking-tight text-zinc-100">Targets</h1>
            <p className="text-[11px] text-zinc-500">{targets.length} campaigns</p>
          </div>
          <Button
            variant="ghost"
            size="icon"
            onClick={onRefresh}
            title="Refresh"
            className="h-7 w-7 text-zinc-400 hover:text-zinc-100"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", isLoading && "animate-spin")} />
          </Button>
        </div>

        {offlineReplaysAvailable === true && (
          <>
            <Button
              className="w-full justify-center gap-1.5 font-medium"
              onClick={() => void handleCreateReplay()}
              disabled={creatingReplay}
            >
              <Play className="h-3 w-3 fill-current" />
              {creatingReplay ? "Creating replay…" : "Run Offline Replay"}
            </Button>
            <p className="text-[11px] text-zinc-500">Canned test data. No external API calls.</p>
            {replayError && (
              <p role="alert" className="text-xs text-rose-400">
                {replayError}
              </p>
            )}
          </>
        )}
        {offlineReplaysAvailable === false && <LaunchPracticeDialog onLaunched={onLaunched} />}

        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search targets..."
          className="h-8 border-zinc-800/80 bg-zinc-900/50 text-xs"
        />
      </div>

      <div className="flex-1 overflow-y-auto py-2">
        {filtered.length === 0 ? (
          <p className="px-4 py-8 text-center text-xs text-zinc-500">No targets yet.</p>
        ) : (
          <div className="space-y-0.5 px-2">
            {filtered.map((item) => {
              const active = item.run_id === selectedRunId;
              const susp = getSuspicionBand(item.latest_suspicion);

              return (
                <button
                  key={item.run_id}
                  type="button"
                  onClick={() => onSelect(item.run_id)}
                  className={cn(
                    "group flex w-full flex-col gap-1 rounded-lg px-3 py-2.5 text-left transition-colors cursor-pointer",
                    active ? "bg-zinc-800/80" : "hover:bg-zinc-900/80"
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span
                      className={cn(
                        "truncate text-xs font-medium",
                        active ? "text-zinc-50" : "text-zinc-200"
                      )}
                    >
                      {item.target_name || item.target_id}
                    </span>
                    <span
                      className={cn(
                        "shrink-0 font-mono text-[10px] font-medium",
                        getStageTextClass(item.state)
                      )}
                    >
                      {item.is_running ? "LIVE" : item.state}
                    </span>
                  </div>

                  <p className="truncate text-[11px] text-zinc-500">
                    {item.target_role || item.target_org || item.run_id}
                  </p>

                  <div className="mt-0.5 flex items-center justify-between font-mono text-[10px] tabular-nums text-zinc-500">
                    <span className={cn("inline-flex items-center gap-1.5", susp.textClass)}>
                      <span
                        className={cn(
                          "h-1.5 w-1.5 shrink-0 rounded-full",
                          item.latest_suspicion === null
                            ? "bg-zinc-600"
                            : item.latest_suspicion >= 0.6
                              ? "bg-rose-500"
                              : item.latest_suspicion >= 0.3
                                ? "bg-zinc-400"
                                : "bg-emerald-400"
                        )}
                      />
                      Suspicion {formatScore(item.latest_suspicion)}
                    </span>
                    <span>
                      {item.touches_used}/{item.session_max}
                    </span>
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </div>

      <div className="flex items-center justify-between border-t border-zinc-800/80 px-4 py-2.5 text-[11px] text-zinc-500">
        <span className="inline-flex items-center gap-1.5">
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              isConnected ? "bg-emerald-400" : "bg-zinc-600"
            )}
          />
          {isConnected ? "Live" : "Polling"}
        </span>
        <span className="font-mono text-[10px]">WAL</span>
      </div>
    </aside>
  );
}
