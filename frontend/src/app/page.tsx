"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ActivityTabs } from "@/components/dashboard/ActivityTabs";
import { KpiStrip } from "@/components/dashboard/KpiStrip";
import { PersonalityEvolutionPanel } from "@/components/dashboard/PersonalityEvolutionPanel";
import { RunExecutionStatus } from "@/components/dashboard/RunExecutionStatus";
import { StagePipeline } from "@/components/dashboard/StagePipeline";
import { SuspicionChart } from "@/components/dashboard/SuspicionChart";
import { TargetSidebar } from "@/components/dashboard/TargetSidebar";
import { VersionScrubber } from "@/components/dashboard/VersionScrubber";
import { WatchoutsCard } from "@/components/dashboard/WatchoutsCard";
import { fetchTargetDetail, fetchTargets, stopPracticeRun } from "@/lib/api";
import type { LaunchRunResponse, TargetDetailResponse, TargetSummary } from "@/lib/types";
import { isMockOrStubRun } from "@/lib/utils";

export default function DashboardPage() {
  const [targets, setTargets] = useState<TargetSummary[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TargetDetailResponse | null>(null);
  const [milestoneIdx, setMilestoneIdx] = useState<number>(0);
  const [isLivePinned, setIsLivePinned] = useState<boolean>(true);
  const [isConnected, setIsConnected] = useState<boolean>(false);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [offlineReplaysAvailable, setOfflineReplaysAvailable] = useState<boolean | null>(null);
  const [stopping, setStopping] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const selectedRunIdRef = useRef<string | null>(null);
  selectedRunIdRef.current = selectedRunId;
  const mainScrollRef = useRef<HTMLElement | null>(null);

  const loadTargets = useCallback(async (preferredRunId?: string) => {
    try {
      const res = await fetchTargets();
      setTargets(res.targets);
      setOfflineReplaysAvailable(res.offline_replays_available === true);
      setError(null);

      const desired = preferredRunId ?? selectedRunIdRef.current;
      if (desired && res.targets.some((t) => t.run_id === desired)) {
        setSelectedRunId(desired);
      } else if (res.targets.length > 0 && !desired) {
        // Prefer the most recent non-mock campaign by default
        const sorted = [...res.targets].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
        const firstReal = sorted.find((t) => !isMockOrStubRun(t)) ?? sorted[0];
        setSelectedRunId(firstReal.run_id);
      }
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Could not reach backend API at /api/targets"
      );
    } finally {
      setIsLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (runId: string) => {
    try {
      const data = await fetchTargetDetail(runId);
      setDetail(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to load ${runId}`);
    }
  }, []);

  // Initial fetch + SSE live stream (debounced updates; fallback poll only if SSE disconnects)
  useEffect(() => {
    void loadTargets();

    let es: EventSource | null = null;
    let sseOpen = false;
    let debounceTimer: ReturnType<typeof setTimeout> | null = null;

    const scheduleRefresh = () => {
      if (debounceTimer) return;
      debounceTimer = setTimeout(() => {
        debounceTimer = null;
        if (document.hidden) return;
        void loadTargets();
        if (selectedRunIdRef.current) {
          void loadDetail(selectedRunIdRef.current);
        }
      }, 300);
    };

    try {
      es = new EventSource("/api/stream");
      es.onopen = () => {
        sseOpen = true;
        setIsConnected(true);
      };
      es.onmessage = (evt) => {
        try {
          const parsed = JSON.parse(evt.data) as { type?: string };
          if (parsed.type === "connected") return;
        } catch {
          // treat non-JSON messages as update notifications
        }
        scheduleRefresh();
      };
      es.onerror = () => {
        sseOpen = false;
        setIsConnected(false);
      };
    } catch {
      sseOpen = false;
      setIsConnected(false);
    }

    const poll = setInterval(() => {
      if (sseOpen || document.hidden) return;
      scheduleRefresh();
    }, 8000);

    const onVisibility = () => {
      if (!document.hidden) {
        scheduleRefresh();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      if (debounceTimer) clearTimeout(debounceTimer);
      clearInterval(poll);
      document.removeEventListener("visibilitychange", onVisibility);
      es?.close();
    };
  }, [loadTargets, loadDetail]);

  // Load detail when selected target changes
  useEffect(() => {
    if (!selectedRunId) {
      setDetail(null);
      return;
    }
    setIsLivePinned(true);
    mainScrollRef.current?.scrollTo({ top: 0 });
    void loadDetail(selectedRunId);
  }, [selectedRunId, loadDetail]);

  // Keep scrubber pinned to latest milestone when live-pinned
  const milestones = useMemo(() => detail?.personality_evolution ?? [], [detail]);
  useEffect(() => {
    if (isLivePinned && milestones.length > 0) {
      setMilestoneIdx(milestones.length - 1);
    }
  }, [isLivePinned, milestones]);

  // Keyboard navigation for timeline scrubber (only when focus isn't inside an interactive widget)
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      const target = e.target as HTMLElement | null;
      if (
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target instanceof HTMLSelectElement ||
        target?.isContentEditable ||
        target?.closest('[role="slider"], [role="tablist"], [role="tab"], [role="dialog"]')
      ) {
        return;
      }
      if (milestones.length <= 1) return;
      if (e.key === "ArrowLeft") {
        setIsLivePinned(false);
        setMilestoneIdx((prev) => Math.max(0, prev - 1));
      } else if (e.key === "ArrowRight") {
        setMilestoneIdx((prev) => {
          const next = Math.min(milestones.length - 1, prev + 1);
          if (next === milestones.length - 1) setIsLivePinned(true);
          return next;
        });
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [milestones.length]);

  const handleSelectVersion = useCallback(
    (targetVersion: number) => {
      if (milestones.length === 0) return;
      let bestIdx = 0;
      for (let i = 0; i < milestones.length; i++) {
        if (milestones[i].version <= targetVersion) {
          bestIdx = i;
        }
      }
      setMilestoneIdx(bestIdx);
      setIsLivePinned(bestIdx === milestones.length - 1);
    },
    [milestones]
  );

  const handleLaunched = useCallback(
    (res: LaunchRunResponse) => {
      setIsLivePinned(true);
      const trySelect = async (attemptsLeft: number) => {
        try {
          const list = await fetchTargets();
          setTargets(list.targets);
          if (list.targets.some((t) => t.run_id === res.run_id)) {
            setSelectedRunId(res.run_id);
            await loadDetail(res.run_id);
            return;
          }
        } catch {
          // retry below
        }
        if (attemptsLeft > 0) {
          setTimeout(() => void trySelect(attemptsLeft - 1), 350);
        }
      };
      setTimeout(() => void trySelect(12), 350);
    },
    [loadDetail]
  );

  const handleStopRun = useCallback(async () => {
    if (!selectedRunId) return;
    setStopping(true);
    try {
      await stopPracticeRun(selectedRunId);
      await loadTargets(selectedRunId);
      await loadDetail(selectedRunId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to stop practice run");
    } finally {
      setStopping(false);
    }
  }, [selectedRunId, loadTargets, loadDetail]);

  const safeIdx = Math.min(Math.max(0, milestoneIdx), Math.max(0, milestones.length - 1));
  const activeMilestone = milestones[safeIdx] ?? null;
  const initialMilestone =
    milestones.find((m) => m.profile.psych.length > 0 || m.profile.style_guide) ??
    milestones[0] ??
    null;

  const displayedVersion = activeMilestone?.version ?? detail?.latest_snapshot.version ?? 0;
  const displayedState = activeMilestone?.state ?? detail?.summary.state ?? "BOOT";
  const displayedSuspicion =
    activeMilestone?.suspicion ?? detail?.summary.latest_suspicion ?? null;
  const displayedEngagement =
    activeMilestone?.engagement ?? detail?.summary.latest_engagement ?? null;
  const displayedMood = activeMilestone?.mood ?? detail?.summary.latest_mood ?? null;
  const displayedProfileConfidence =
    activeMilestone?.profile.overall_confidence ?? detail?.summary.profile_confidence ?? 0;

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      <TargetSidebar
        targets={targets}
        selectedRunId={selectedRunId}
        onSelect={(id) => setSelectedRunId(id)}
        onRefresh={() => {
          setIsLoading(true);
          void loadTargets();
          if (selectedRunId) void loadDetail(selectedRunId);
        }}
        onLaunched={handleLaunched}
        offlineReplaysAvailable={offlineReplaysAvailable}
        onReplayCreated={(runId) => void loadTargets(runId)}
        isConnected={isConnected}
        isLoading={isLoading}
      />

      <main ref={mainScrollRef} className="flex-1 overflow-y-auto px-8 py-6">
        {error && (
          <div className="mx-auto mb-5 max-w-[1360px] rounded-lg border border-rose-900/50 bg-rose-950/20 px-4 py-2.5 text-xs text-rose-200">
            {error} — ensure the FastAPI backend is running (
            <code className="font-mono text-rose-100">uv run python -m src.web.server</code>).
          </div>
        )}

        {!detail ? (
          isLoading ? (
            <div className="mx-auto max-w-[1360px] space-y-5 animate-pulse">
              <div className="h-20 rounded-xl bg-card" />
              <div className="h-24 rounded-xl bg-card" />
              <div className="h-12 rounded-xl bg-card" />
              <div className="grid grid-cols-12 gap-5">
                <div className="col-span-6 h-80 rounded-xl bg-card" />
                <div className="col-span-6 h-80 rounded-xl bg-card" />
              </div>
            </div>
          ) : (
            <div className="mx-auto flex h-[75vh] max-w-[1360px] flex-col items-center justify-center rounded-xl border border-dashed border-zinc-800 bg-card/40 p-8 text-center">
              <h2 className="text-sm font-medium text-zinc-200">No campaign selected</h2>
              <p className="mt-1.5 max-w-md text-xs text-zinc-400">
                Choose a campaign from the sidebar or start a practice run. To generate sample
                replays, run:
              </p>
              <code className="mt-3 rounded-lg border border-zinc-800 bg-background px-3 py-1.5 font-mono text-xs text-zinc-300">
                uv run python -m src.web.demo_seed --data-dir data
              </code>
            </div>
          )
        ) : (
          <div className="mx-auto max-w-[1360px] space-y-5">
            <StagePipeline
              summary={detail.summary}
              stageHistory={detail.stage_history}
              activeState={displayedState}
              onStopRun={handleStopRun}
              stopping={stopping}
            />

            {detail.summary.is_running && (
              <RunExecutionStatus
                key={`exec-live-${detail.run_id}`}
                executionStatus={detail.execution_status}
                state={detail.summary.state}
              />
            )}

            <KpiStrip
              touchesUsed={
                isLivePinned
                  ? detail.summary.touches_used
                  : (activeMilestone?.touch_n ?? detail.summary.touches_used)
              }
              sessionMax={detail.summary.session_max}
              sessionsUsed={detail.summary.sessions_used}
              sessionsAllowed={detail.summary.sessions_allowed}
              suspicion={displayedSuspicion}
              engagement={displayedEngagement}
              mood={displayedMood}
              profileConfidence={displayedProfileConfidence}
              incidentsCount={detail.summary.incidents_count}
            />

            <VersionScrubber
              milestones={milestones}
              selectedIndex={safeIdx}
              isLivePinned={isLivePinned}
              isRunning={detail.summary.is_running}
              onSelectIndex={(idx) => {
                setMilestoneIdx(idx);
                setIsLivePinned(idx === milestones.length - 1);
              }}
              onJumpLive={() => {
                setIsLivePinned(true);
                setMilestoneIdx(Math.max(0, milestones.length - 1));
              }}
            />

            {/* Primary Analytical Grid: Suspicion & Signals vs Personality Evolution */}
            <div className="grid grid-cols-12 items-start gap-5">
              <div className="col-span-6 space-y-5">
                <SuspicionChart
                  key={`chart-${detail.run_id}`}
                  signals={detail.signal_series}
                  markers={detail.event_markers}
                  selectedVersion={displayedVersion}
                  onSelectVersion={handleSelectVersion}
                />
                <WatchoutsCard
                  watchouts={detail.watchout_history}
                  activeWatchouts={
                    activeMilestone?.profile.watchouts ?? detail.latest_snapshot.profile.watchouts
                  }
                  selectedVersion={displayedVersion}
                  onSelectVersion={handleSelectVersion}
                />
              </div>

              <div className="col-span-6">
                <PersonalityEvolutionPanel
                  key={`personality-${detail.run_id}`}
                  initialMilestone={initialMilestone}
                  currentMilestone={activeMilestone}
                  factsById={detail.facts_by_id}
                  events={detail.events}
                  onSelectVersion={handleSelectVersion}
                />
              </div>
            </div>

            <ActivityTabs
              key={`activity-${detail.run_id}`}
              runId={detail.run_id}
              targetName={detail.summary.target_name || detail.summary.target_id}
              senderName={detail.summary.sender_name}
              selectedVersion={displayedVersion}
              turns={detail.turns}
              events={detail.events}
              decisions={detail.decisions}
              facts={detail.latest_snapshot.facts}
              factsById={detail.facts_by_id}
              signals={detail.signal_series}
              registeredPages={detail.registered_pages}
              assetsById={detail.assets_by_id}
              onSelectVersion={handleSelectVersion}
            />

            {!detail.summary.is_running && (
              <RunExecutionStatus
                key={`exec-done-${detail.run_id}`}
                executionStatus={detail.execution_status}
                state={detail.summary.state}
              />
            )}
          </div>
        )}
      </main>
    </div>
  );
}
