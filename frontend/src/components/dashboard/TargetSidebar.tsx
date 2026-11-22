"use client";

import { RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { LaunchPracticeDialog } from "@/components/dashboard/LaunchPracticeDialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { createOfflineReplay } from "@/lib/api";
import type { LaunchRunResponse, TargetSummary } from "@/lib/types";
import {
  cn,
  formatAgentStage,
  formatDuration,
  formatRunTag,
  formatScore,
  formatStageState,
  getStageTextClass,
  getSuspicionBand,
  isMockOrStubRun,
} from "@/lib/utils";

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
  const [showMocks, setShowMocks] = useState(false);
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

  const { visibleTargets, hiddenMockCount } = useMemo(() => {
    const q = search.trim().toLowerCase();
    const sorted = [...targets].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    let mockHidden = 0;

    const list = sorted.filter((t) => {
      const isMock = isMockOrStubRun(t);
      if (isMock && !showMocks && !q && t.run_id !== selectedRunId) {
        mockHidden += 1;
        return false;
      }
      if (!q) return true;
      const runTag = formatRunTag(t.run_id) ?? "";
      return (
        t.target_name.toLowerCase().includes(q) ||
        t.target_id.toLowerCase().includes(q) ||
        t.target_org.toLowerCase().includes(q) ||
        t.run_id.toLowerCase().includes(q) ||
        runTag.toLowerCase().includes(q)
      );
    });

    return { visibleTargets: list, hiddenMockCount: mockHidden };
  }, [targets, search, showMocks, selectedRunId]);

  const totalRealCount = targets.filter((t) => !isMockOrStubRun(t)).length;
  const displayCount = showMocks ? targets.length : totalRealCount || targets.length;

  return (
    <aside className="flex h-full w-72 shrink-0 flex-col border-r border-zinc-800/80 bg-card/60">
      <div className="space-y-3 border-b border-zinc-800/80 p-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-sm font-medium tracking-tight text-zinc-100">Campaigns</h2>
            <p className="text-xs text-zinc-400">
              {displayCount} {displayCount === 1 ? "run" : "runs"}
            </p>
          </div>
          <Button
            variant="ghost"
            size="icon"
            onClick={onRefresh}
            aria-label="Refresh campaigns"
            className="h-7 w-7 text-zinc-400 hover:text-zinc-100"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", isLoading && "animate-spin")} />
          </Button>
        </div>

        {offlineReplaysAvailable === true && (
          <>
            <Button
              variant="outline"
              className="w-full justify-center font-medium text-zinc-200"
              onClick={() => void handleCreateReplay()}
              disabled={creatingReplay}
            >
              {creatingReplay ? "Creating replay…" : "Run Offline Replay"}
            </Button>
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
          aria-label="Search targets"
          className="h-8 border-zinc-800/80 bg-background/70 text-xs"
        />
      </div>

      <div className="flex-1 overflow-y-auto py-2">
        {visibleTargets.length === 0 ? (
          <p className="px-4 py-8 text-center text-xs text-zinc-400">
            {search.trim() ? "No matching campaigns." : "No campaigns yet."}
          </p>
        ) : (
          <div className="space-y-0.5 px-2">
            {visibleTargets.map((item) => {
              const active = item.run_id === selectedRunId;
              const susp = getSuspicionBand(item.latest_suspicion);
              const runTag = formatRunTag(item.run_id);
              const isMock = isMockOrStubRun(item);

              return (
                <button
                  key={item.run_id}
                  type="button"
                  onClick={() => onSelect(item.run_id)}
                  className={cn(
                    "group flex w-full cursor-pointer flex-col gap-1 rounded-lg px-3 py-2.5 text-left transition-colors",
                    active ? "bg-card-elevated" : "hover:bg-card-elevated/50"
                  )}
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <div className="flex min-w-0 items-baseline gap-1.5">
                      <span
                        className={cn(
                          "truncate text-[13px] font-medium",
                          active ? "text-zinc-50" : "text-zinc-200"
                        )}
                      >
                        {item.target_name || item.target_id}
                      </span>
                      {runTag && (
                        <span className="shrink-0 text-[11px] text-zinc-400">{runTag}</span>
                      )}
                      {isMock && (
                        <span className="shrink-0 text-[11px] text-zinc-500">· Mock</span>
                      )}
                    </div>
                    <span
                      className={cn(
                        "shrink-0 text-xs font-medium",
                        item.is_running ? "text-emerald-400" : getStageTextClass(item.state)
                      )}
                    >
                      {item.is_running ? "Live" : formatStageState(item.state)}
                    </span>
                  </div>

                  <p className="truncate text-xs text-zinc-400">
                    {item.target_role || item.target_org || item.run_id}
                  </p>

                  <div className="mt-0.5 flex items-center justify-between text-[11px] text-zinc-400">
                    <span className="inline-flex items-center gap-1.5">
                      <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", susp.dotClass)} />
                      <span>Suspicion</span>
                      <span className={cn("font-mono tabular-nums", susp.textClass)}>
                        {formatScore(item.latest_suspicion)}
                      </span>
                    </span>
                    <span className="inline-flex items-center gap-2 font-mono tabular-nums text-zinc-400">
                      {item.is_running && item.active_stage ? (
                        <span className="font-sans text-emerald-400">
                          {formatAgentStage(item.active_stage)}
                        </span>
                      ) : item.total_elapsed_ms !== null && item.total_elapsed_ms !== undefined ? (
                        <span>{formatDuration(item.total_elapsed_ms)}</span>
                      ) : null}
                      <span>
                        {item.touches_used}/{item.session_max}
                      </span>
                    </span>
                  </div>
                </button>
              );
            })}
          </div>
        )}

        {(hiddenMockCount > 0 || showMocks) && !search.trim() && (
          <div className="mt-2 px-4 py-1.5">
            <button
              type="button"
              onClick={() => setShowMocks((prev) => !prev)}
              className="cursor-pointer text-[11px] text-zinc-500 transition-colors hover:text-zinc-300"
            >
              {showMocks
                ? "Hide mock runs"
                : `${hiddenMockCount} mock ${hiddenMockCount === 1 ? "run" : "runs"} hidden · Show`}
            </button>
          </div>
        )}
      </div>

      <div className="flex items-center justify-between border-t border-zinc-800/80 px-4 py-2.5 text-xs text-zinc-400">
        <span className="inline-flex items-center gap-2">
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              isConnected ? "bg-emerald-400" : "bg-zinc-600"
            )}
          />
          {isConnected ? "Live sync" : "Polling"}
        </span>
      </div>
    </aside>
  );
}
