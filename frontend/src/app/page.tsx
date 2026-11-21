"use client";

import { AlertCircle, Terminal } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ActivityTabs } from "@/components/dashboard/ActivityTabs";
import { KpiStrip } from "@/components/dashboard/KpiStrip";
import { PersonalityEvolutionPanel } from "@/components/dashboard/PersonalityEvolutionPanel";
import { StagePipeline } from "@/components/dashboard/StagePipeline";
import { SuspicionChart } from "@/components/dashboard/SuspicionChart";
import { TargetSidebar } from "@/components/dashboard/TargetSidebar";
import { VersionScrubber } from "@/components/dashboard/VersionScrubber";
import { WatchoutsCard } from "@/components/dashboard/WatchoutsCard";
import { fetchTargetDetail, fetchTargets, stopPracticeRun } from "@/lib/api";
import type { LaunchRunResponse, TargetDetailResponse, TargetSummary } from "@/lib/types";

export default function DashboardPage() {
  const [targets, setTargets] = useState<TargetSummary[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TargetDetailResponse | null>(null);
  const [milestoneIdx, setMilestoneIdx] = useState<number>(0);
  const [isLivePinned, setIsLivePinned] = useState<boolean>(true);
  const [isConnected, setIsConnected] = useState<boolean>(false);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [stopping, setStopping] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const selectedRunIdRef = useRef<string | null>(null);
  selectedRunIdRef.current = selectedRunId;

  const loadTargets = useCallback(async (preferredRunId?: string) => {
    try {
      const res = await fetchTargets();
      setTargets(res.targets);
      setError(null);

      const desired = preferredRunId ?? selectedRunIdRef.current;
      if (desired && res.targets.some((t) => t.run_id === desired)) {
        setSelectedRunId(desired);
      } else if (res.targets.length > 0 && !desired) {
        setSelectedRunId(res.targets[0].run_id);
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

  // Initial fetch + SSE live stream with 2.5s polling fallback
  useEffect(() => {
    void loadTargets();

    let es: EventSource | null = null;
    try {
      es = new EventSource("/api/stream");
      es.onopen = () => setIsConnected(true);
      es.onmessage = () => {
        void loadTargets();
        if (selectedRunIdRef.current) {
          void loadDetail(selectedRunIdRef.current);
        }
      };
      es.onerror = () => {
        setIsConnected(false);
      };
    } catch {
      setIsConnected(false);
    }

    const poll = setInterval(() => {
      void loadTargets();
      if (selectedRunIdRef.current) {
        void loadDetail(selectedRunIdRef.current);
      }
    }, 2500);

    return () => {
      clearInterval(poll);
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
    void loadDetail(selectedRunId);
  }, [selectedRunId, loadDetail]);

  // Keep scrubber pinned to latest milestone when live-pinned
  const milestones = useMemo(() => detail?.personality_evolution ?? [], [detail]);
  useEffect(() => {
    if (isLivePinned && milestones.length > 0) {
      setMilestoneIdx(milestones.length - 1);
    }
  }, [isLivePinned, milestones]);

  // Keyboard navigation for timeline scrubber
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement ||
        e.target instanceof HTMLSelectElement
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
    <div className="flex min-h-screen flex-col bg-[#09090B] lg:h-screen lg:flex-row lg:overflow-hidden">
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
        isConnected={isConnected}
        isLoading={isLoading}
      />

      <main className="flex-1 overflow-y-auto p-5 lg:p-7">
        {error && (
          <div className="mb-5 flex items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-900/70 px-4 py-2.5 text-xs text-zinc-300">
            <AlertCircle className="h-3.5 w-3.5 shrink-0 text-rose-400" />
            <span>{error} — ensure FastAPI backend is running (`uv run python -m src.web.server`).</span>
          </div>
        )}

        {!detail ? (
          <div className="flex h-[75vh] flex-col items-center justify-center rounded-xl border border-dashed border-zinc-800 bg-[#111114]/50 p-8 text-center">
            <Terminal className="h-6 w-6 text-zinc-500" />
            <h2 className="mt-3 text-sm font-medium text-zinc-200">
              {isLoading ? "Loading campaigns..." : "No target campaign selected"}
            </h2>
            <p className="mt-1 max-w-md text-xs text-zinc-500">
              Click Launch Practice Run in the sidebar or seed sample campaigns:
            </p>
            <code className="mt-3 rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-1.5 font-mono text-xs text-zinc-300">
              uv run python -m src.web.demo_seed --data-dir data
            </code>
          </div>
        ) : (
          <div className="mx-auto max-w-[1320px] space-y-5">
            <StagePipeline
              summary={detail.summary}
              stageHistory={detail.stage_history}
              activeState={displayedState}
              onStopRun={handleStopRun}
              stopping={stopping}
            />

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
            <div className="grid grid-cols-1 gap-5 xl:grid-cols-12">
              <div className="space-y-5 xl:col-span-6">
                <SuspicionChart
                  signals={detail.signal_series}
                  markers={detail.event_markers}
                  selectedVersion={displayedVersion}
                  onSelectVersion={handleSelectVersion}
                />
                <WatchoutsCard
                  watchouts={detail.watchout_history}
                  activeWatchouts={activeMilestone?.profile.watchouts ?? detail.latest_snapshot.profile.watchouts}
                  selectedVersion={displayedVersion}
                  onSelectVersion={handleSelectVersion}
                />
              </div>

              <div className="xl:col-span-6">
                <PersonalityEvolutionPanel
                  initialMilestone={initialMilestone}
                  currentMilestone={activeMilestone}
                  factsById={detail.facts_by_id}
                />
              </div>
            </div>

            <ActivityTabs
              runId={detail.run_id}
              turns={detail.turns}
              events={detail.events}
              decisions={detail.decisions}
              facts={detail.latest_snapshot.facts}
              signals={detail.signal_series}
              registeredPages={detail.registered_pages}
              assetsById={detail.assets_by_id}
              onSelectVersion={handleSelectVersion}
            />
          </div>
        )}
      </main>
    </div>
  );
}
