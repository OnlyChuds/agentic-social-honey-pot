"use client";

import { AlertTriangle, Check, Loader2, ShieldAlert } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { fetchEvalScenarios, fetchEvalStatus, launchEvalRuns } from "@/lib/api";
import type {
  EvalScenariosResponse,
  EvalStatusResponse,
  LaunchRunResponse,
} from "@/lib/types";

interface LaunchEvalDialogProps {
  onLaunched: (res: LaunchRunResponse) => void;
}

export function LaunchEvalDialog({ onLaunched }: LaunchEvalDialogProps) {
  const [open, setOpen] = useState(false);
  const [evalStatus, setEvalStatus] = useState<EvalStatusResponse | null>(null);
  const [scenarioMeta, setScenarioMeta] = useState<EvalScenariosResponse | null>(null);
  const [loadingTargets, setLoadingTargets] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [manualTargets, setManualTargets] = useState("");
  const [runAll, setRunAll] = useState(true);
  const [zeroPacing, setZeroPacing] = useState(true);
  const [confirmed, setConfirmed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Only read local disk/process eval status when dialog opens — never calls Target API.
  useEffect(() => {
    if (!open) return;
    setConfirmed(false);
    setError(null);
    void fetchEvalStatus()
      .then((status) => setEvalStatus(status))
      .catch(() => {
        // Ignore transient status read failure
      });
  }, [open]);

  const targetCounts = evalStatus?.target_counts ?? scenarioMeta?.target_counts ?? {};
  const maxPerTarget = evalStatus?.max_per_target ?? scenarioMeta?.max_per_target ?? 5;
  const remaining = evalStatus?.remaining ?? scenarioMeta?.remaining ?? 15;
  const maxSessions = evalStatus?.max_sessions ?? scenarioMeta?.max_sessions ?? 15;

  async function handleLoadTargets() {
    setLoadingTargets(true);
    setError(null);
    try {
      const meta = await fetchEvalScenarios();
      setScenarioMeta(meta);
      setEvalStatus({
        used_targets: meta.used_targets,
        target_counts: meta.target_counts,
        max_per_target: meta.max_per_target,
        used_count: meta.used_count,
        max_sessions: meta.max_sessions,
        remaining: meta.remaining,
      });
      const counts = meta.target_counts ?? {};
      const limit = meta.max_per_target ?? 5;
      const available = meta.targets
        .map((t) => t.id)
        .filter((id) => (counts[id] ?? 0) < limit);
      setSelectedIds(available);
      setRunAll(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load eval targets");
    } finally {
      setLoadingTargets(false);
    }
  }

  function toggleTarget(id: string) {
    if ((targetCounts[id] ?? 0) >= maxPerTarget) return;
    setRunAll(false);
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]
    );
  }

  function parsedManualIds(): string[] {
    return manualTargets
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!confirmed) {
      setError("Please confirm before spending real Eval session(s).");
      return;
    }

    const manual = parsedManualIds();
    const targetIds = manual.length > 0 ? manual : runAll ? [] : selectedIds;
    if (!runAll && targetIds.length === 0) {
      setError("Select at least one eval target, enter target ID(s), or choose Run all.");
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      const res = await launchEvalRuns({
        target_ids: targetIds,
        run_all: runAll && manual.length === 0,
        zero_pacing: zeroPacing,
        aggressive: true,
        confirm: true,
      });
      setEvalStatus(res.eval_status);
      setOpen(false);
      if (res.runs.length > 0) {
        onLaunched(res.runs[0]);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to launch eval run");
    } finally {
      setSubmitting(false);
    }
  }

  const manualIds = parsedManualIds();
  const launchCountLabel =
    manualIds.length > 0
      ? `${manualIds.length} target${manualIds.length === 1 ? "" : "s"}`
      : runAll
        ? "1 try per persona"
        : `${selectedIds.length} selected`;

  const canSubmit =
    !submitting &&
    confirmed &&
    remaining > 0 &&
    (manualIds.length > 0 || runAll || selectedIds.length > 0);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          variant="outline"
          className="w-full justify-center gap-1.5 border-amber-500/40 bg-amber-500/10 font-medium text-amber-200 hover:border-amber-400/60 hover:bg-amber-500/20 hover:text-amber-100"
          size="default"
        >
          <ShieldAlert className="h-3.5 w-3.5" />
          Run Eval
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-amber-100">
            <ShieldAlert className="h-4 w-4 text-amber-400" />
            Run Scored Evaluation
          </DialogTitle>
          <DialogDescription>
            3 eval personas · {maxPerTarget} tries each ({maxSessions} sessions total) · Best 2
            runs per persona scored · 10 touches/session.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="rounded-lg border border-amber-500/30 bg-amber-950/30 px-3 py-2.5 text-xs text-amber-100">
            <div className="flex items-center justify-between font-medium">
              <span className="inline-flex items-center gap-1.5">
                <AlertTriangle className="h-3.5 w-3.5 text-amber-400" />
                Eval Budget Guard
              </span>
              <span className="font-mono tabular-nums text-amber-300">
                {remaining}/{maxSessions} tries left
              </span>
            </div>
            <p className="mt-1 text-[11px] leading-relaxed text-amber-200/80">
              Each run is locked to 1 session (--max-sessions 1, 10 touches) and saves learned
              target memory for subsequent tries (3 throwaway + 2 best scored per persona).
            </p>
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-zinc-300">Eval Targets</span>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => void handleLoadTargets()}
                disabled={loadingTargets || submitting}
                className="h-7 px-2.5 text-[11px]"
              >
                {loadingTargets ? (
                  <>
                    <Loader2 className="h-3 w-3 animate-spin" />
                    Loading…
                  </>
                ) : scenarioMeta ? (
                  "Reload Eval Targets"
                ) : (
                  "Load Eval Targets"
                )}
              </Button>
            </div>

            {scenarioMeta ? (
              <div className="space-y-1.5">
                {scenarioMeta.company && (
                  <p className="text-[11px] text-zinc-400">
                    Org: <span className="font-medium text-zinc-200">{scenarioMeta.company}</span>{" "}
                    · {scenarioMeta.touch_budget} touches/session ·{" "}
                    {scenarioMeta.open ? "Open" : "Closed"}
                  </p>
                )}
                {scenarioMeta.targets.map((target) => {
                  const usedCount = targetCounts[target.id] ?? 0;
                  const triesLeft = Math.max(0, maxPerTarget - usedCount);
                  const exhausted = triesLeft === 0;
                  const checked = !exhausted && selectedIds.includes(target.id);
                  return (
                    <button
                      key={target.id}
                      type="button"
                      disabled={exhausted}
                      onClick={() => toggleTarget(target.id)}
                      className={`flex w-full items-center justify-between rounded-lg border px-3 py-2 text-left text-xs transition-colors ${
                        exhausted
                          ? "cursor-not-allowed border-zinc-800/60 bg-zinc-950/60 text-zinc-500"
                          : checked
                            ? "cursor-pointer border-amber-500/50 bg-amber-950/30 text-zinc-50"
                            : "cursor-pointer border-zinc-800 bg-zinc-900/50 text-zinc-300 hover:border-zinc-700"
                      }`}
                    >
                      <div className="min-w-0 pr-2">
                        <div className="truncate font-medium">
                          {target.name}{" "}
                          <span className="font-mono text-[11px] text-zinc-400">({target.id})</span>
                        </div>
                        {target.role && (
                          <div className="truncate text-[11px] text-zinc-400">{target.role}</div>
                        )}
                      </div>
                      {exhausted ? (
                        <span className="shrink-0 rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">
                          {usedCount}/{maxPerTarget} used
                        </span>
                      ) : (
                        <span className="inline-flex shrink-0 items-center gap-1.5">
                          <span className="font-mono text-[10px] text-zinc-400">
                            {triesLeft}/{maxPerTarget} left
                          </span>
                          {checked && <Check className="h-4 w-4 text-amber-400" />}
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
            ) : (
              <div className="space-y-2">
                <label className="flex cursor-pointer items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-900/50 px-3 py-2 text-xs text-zinc-200">
                  <input
                    type="checkbox"
                    checked={runAll && manualIds.length === 0}
                    onChange={(e) => {
                      setRunAll(e.target.checked);
                      if (e.target.checked) setManualTargets("");
                    }}
                    className="rounded border-zinc-700 bg-zinc-900 accent-amber-400"
                  />
                  <span>Run 1 try on all eligible eval personas</span>
                </label>
                <p className="text-[11px] text-zinc-500">
                  No API calls are made until you click &quot;Load Eval Targets&quot; or start the
                  run.
                </p>
              </div>
            )}

            <Input
              id="eval-manual-targets"
              value={manualTargets}
              onChange={(e) => {
                setManualTargets(e.target.value);
                if (e.target.value.trim()) setRunAll(false);
              }}
              placeholder="Or enter eval target ID(s), comma-separated..."
              className="mt-1"
            />
          </div>

          <div className="flex items-center justify-between rounded-lg border border-zinc-800 bg-zinc-900/50 px-3 py-2 text-xs">
            <label className="flex cursor-pointer items-center gap-2 text-zinc-300">
              <input
                type="checkbox"
                checked={zeroPacing}
                onChange={(e) => setZeroPacing(e.target.checked)}
                className="rounded border-zinc-700 bg-zinc-900 accent-amber-400"
              />
              Zero pacing
            </label>
            <span className="text-zinc-400">
              10 touches · {maxPerTarget} tries/person (best 2 scored)
            </span>
          </div>

          <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-rose-900/50 bg-rose-950/20 px-3 py-2 text-xs text-rose-100">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
              className="mt-0.5 rounded border-rose-700 bg-zinc-900 accent-amber-400"
            />
            <span>
              I confirm launching real scored Eval run(s) ({launchCountLabel}). Uses 1 of{" "}
              {maxPerTarget} tries per selected persona.
            </span>
          </label>

          {error && (
            <p className="rounded-lg border border-rose-900/60 bg-rose-950/40 px-3 py-2 text-xs text-rose-200">
              {error}
            </p>
          )}

          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={() => setOpen(false)}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={!canSubmit}
              className="bg-amber-500 text-zinc-950 hover:bg-amber-400"
            >
              {submitting ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  Launching Eval…
                </>
              ) : (
                `Start Eval (${launchCountLabel})`
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
