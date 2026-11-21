"use client";

import { Play, Loader2, ShieldAlert } from "lucide-react";
import { useState } from "react";
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
import { launchPracticeRun } from "@/lib/api";
import type { LaunchRunRequest, LaunchRunResponse } from "@/lib/types";

interface LaunchPracticeDialogProps {
  onLaunched: (res: LaunchRunResponse) => void;
}

const PRESET_TARGETS = [
  { id: "julius", label: "Julius Muth — Co-CEO (GTM)" },
  { id: "tom", label: "Tom Müller — Co-CEO (Product)" },
  { id: "robert", label: "Robert Seilbeck — CTO" },
  { id: "silent_target", label: "Alex Quiet — VP Ops (Mock Silent)" },
];

export function LaunchPracticeDialog({ onLaunched }: LaunchPracticeDialogProps) {
  const [open, setOpen] = useState(false);
  const [targetId, setTargetId] = useState("julius");
  const [customTarget, setCustomTarget] = useState("");
  const [mode, setMode] = useState<LaunchRunRequest["mode"]>("mock_stub");
  const [objective, setObjective] =
    useState<LaunchRunRequest["stub_objective"]>("credentials");
  const [zeroPacing, setZeroPacing] = useState(true);
  const [aggressive, setAggressive] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const resolvedTarget = (customTarget.trim() || targetId).trim();
    if (!resolvedTarget) {
      setError("Please choose or enter a target ID.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const res = await launchPracticeRun({
        target_id: resolvedTarget,
        mode,
        stub_objective: objective,
        zero_pacing: zeroPacing,
        aggressive,
      });
      setOpen(false);
      onLaunched(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to launch practice run");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button className="w-full justify-center gap-1.5 font-medium" size="default">
          <Play className="h-3 w-3 fill-current" />
          Launch Practice Run
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Launch Practice Campaign</DialogTitle>
          <DialogDescription>
            Spawns a sandboxed practice run writing live to SQLite WAL. Eval runs are locked to CLI
            only.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <label className="text-[11px] font-medium uppercase tracking-wider text-zinc-400">
              Target Persona
            </label>
            <div className="grid grid-cols-1 gap-1.5">
              {PRESET_TARGETS.map((preset) => {
                const selected = !customTarget.trim() && targetId === preset.id;
                return (
                  <button
                    key={preset.id}
                    type="button"
                    onClick={() => {
                      setTargetId(preset.id);
                      setCustomTarget("");
                    }}
                    className={`flex items-center justify-between rounded-lg border px-3 py-2 text-left text-xs transition-colors cursor-pointer ${
                      selected
                        ? "border-zinc-500 bg-zinc-800/90 text-zinc-50"
                        : "border-zinc-800 bg-zinc-900/60 text-zinc-300 hover:border-zinc-700"
                    }`}
                  >
                    <span>{preset.label}</span>
                    <span className="font-mono text-[10px] text-zinc-400">{preset.id}</span>
                  </button>
                );
              })}
            </div>
            <Input
              value={customTarget}
              onChange={(e) => setCustomTarget(e.target.value)}
              placeholder="Or type custom target ID..."
              className="mt-1.5"
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <label className="text-[11px] font-medium uppercase tracking-wider text-zinc-400">
                Execution Mode
              </label>
              <select
                value={mode}
                onChange={(e) => setMode(e.target.value as LaunchRunRequest["mode"])}
                className="h-8 w-full rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 text-xs text-zinc-100 focus:outline-none focus:ring-1 focus:ring-zinc-500"
              >
                <option value="mock_stub">Mock API + Stub Agents</option>
                <option value="practice_live">Live Practice API</option>
              </select>
            </div>

            <div className="space-y-1.5">
              <label className="text-[11px] font-medium uppercase tracking-wider text-zinc-400">
                Objective (Stub)
              </label>
              <select
                value={objective}
                disabled={mode !== "mock_stub"}
                onChange={(e) =>
                  setObjective(e.target.value as LaunchRunRequest["stub_objective"])
                }
                className="h-8 w-full rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 text-xs text-zinc-100 focus:outline-none focus:ring-1 focus:ring-zinc-500 disabled:opacity-40"
              >
                <option value="credentials">Credentials (Landing Page)</option>
                <option value="action">Action (Attachment)</option>
                <option value="disclosure">Disclosure (Internal Info)</option>
              </select>
            </div>
          </div>

          <div className="flex items-center justify-between rounded-lg border border-zinc-800 bg-zinc-900/50 px-3 py-2 text-xs">
            <label className="flex items-center gap-2 text-zinc-300 cursor-pointer">
              <input
                type="checkbox"
                checked={zeroPacing}
                onChange={(e) => setZeroPacing(e.target.checked)}
                className="rounded border-zinc-700 bg-zinc-900 accent-zinc-100"
              />
              Zero pacing (fast demo)
            </label>
            <label className="flex items-center gap-2 text-zinc-300 cursor-pointer">
              <input
                type="checkbox"
                checked={aggressive}
                onChange={(e) => setAggressive(e.target.checked)}
                className="rounded border-zinc-700 bg-zinc-900 accent-zinc-100"
              />
              Aggressive budget
            </label>
          </div>

          {error && (
            <div className="flex items-center gap-2 rounded-lg border border-rose-800/70 bg-rose-950/60 px-3 py-2 text-xs text-rose-200">
              <ShieldAlert className="h-4 w-4 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setOpen(false)}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={submitting}>
              {submitting ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  Starting...
                </>
              ) : (
                <>
                  <Play className="h-3.5 w-3.5 fill-current" />
                  Start Run
                </>
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
