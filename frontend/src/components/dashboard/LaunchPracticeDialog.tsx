"use client";

import { Loader2 } from "lucide-react";
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
import type { LaunchRunResponse } from "@/lib/types";

interface LaunchPracticeDialogProps {
  onLaunched: (res: LaunchRunResponse) => void;
}

const PRESET_TARGETS = [
  { id: "julius", label: "Julius Muth", role: "Co-CEO (GTM)" },
  { id: "tom", label: "Tom Müller", role: "Co-CEO (Product)" },
  { id: "robert", label: "Robert Seilbeck", role: "CTO" },
];

export function LaunchPracticeDialog({ onLaunched }: LaunchPracticeDialogProps) {
  const [open, setOpen] = useState(false);
  const [targetId, setTargetId] = useState("julius");
  const [customTarget, setCustomTarget] = useState("");
  const [zeroPacing, setZeroPacing] = useState(true);
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
        mode: "practice_live",
        zero_pacing: zeroPacing,
        aggressive: true,
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
        <Button
          variant="outline"
          className="w-full justify-center font-medium text-zinc-200"
          size="default"
        >
          New Practice Run
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>New Practice Campaign</DialogTitle>
          <DialogDescription>
            Starts a sandboxed practice run (unlimited sessions, unscored).
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <label htmlFor="custom-target-id" className="text-xs font-medium text-zinc-300">
              Target
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
                    className={`flex cursor-pointer items-center justify-between rounded-lg border px-3 py-2 text-left text-xs transition-colors ${
                      selected
                        ? "border-zinc-500 bg-zinc-800/80 text-zinc-50"
                        : "border-zinc-800 bg-zinc-900/50 text-zinc-300 hover:border-zinc-700"
                    }`}
                  >
                    <span className="font-medium">{preset.label}</span>
                    <span className="text-zinc-400">{preset.role}</span>
                  </button>
                );
              })}
            </div>
            <Input
              id="custom-target-id"
              value={customTarget}
              onChange={(e) => setCustomTarget(e.target.value)}
              placeholder="Or enter custom target ID..."
              className="mt-1.5"
            />
          </div>

          <div className="flex items-center justify-between rounded-lg border border-zinc-800 bg-zinc-900/50 px-3 py-2 text-xs">
            <label className="flex cursor-pointer items-center gap-2 text-zinc-300">
              <input
                type="checkbox"
                checked={zeroPacing}
                onChange={(e) => setZeroPacing(e.target.checked)}
                className="rounded border-zinc-700 bg-zinc-900 accent-zinc-100"
              />
              Zero pacing
            </label>
            <span className="text-zinc-400">10-touch budget</span>
          </div>

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
            <Button type="submit" disabled={submitting}>
              {submitting ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  Starting...
                </>
              ) : (
                "Start Run"
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
