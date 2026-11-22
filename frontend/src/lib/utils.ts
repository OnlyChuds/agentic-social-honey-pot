import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import type { CampaignState } from "@/lib/types";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export interface SuspicionBand {
  label: "Low" | "Guarded" | "High" | "—";
  textClass: string;
  dotClass: string;
}

export function getSuspicionBand(value: number | null | undefined): SuspicionBand {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return {
      label: "—",
      textClass: "text-zinc-500",
      dotClass: "bg-zinc-600",
    };
  }
  if (value >= 0.6) {
    return {
      label: "High",
      textClass: "text-rose-400",
      dotClass: "bg-rose-500",
    };
  }
  if (value >= 0.3) {
    return {
      label: "Guarded",
      textClass: "text-zinc-200",
      dotClass: "bg-zinc-400",
    };
  }
  return {
    label: "Low",
    textClass: "text-zinc-400",
    dotClass: "bg-emerald-400",
  };
}

export function getStageTextClass(stage: CampaignState): string {
  switch (stage) {
    case "WIN":
      return "text-emerald-400";
    case "ABORT":
      return "text-rose-400";
    case "ENGAGE":
      return "text-zinc-100";
    default:
      return "text-zinc-400";
  }
}

export function formatTraitLabel(raw: string): string {
  const cleaned = raw.replaceAll("_", " ").trim();
  if (!cleaned) return raw;
  return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
}

export function formatScore(val: number | null | undefined): string {
  if (val === null || val === undefined || Number.isNaN(val)) return "—";
  return val.toFixed(2);
}

export function formatPercent(val: number | null | undefined): string {
  if (val === null || val === undefined || Number.isNaN(val)) return "—";
  return `${Math.round(val * 100)}%`;
}

export function formatTimeShort(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(11, 16) || iso;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const totalSec = ms / 1000;
  if (totalSec < 60) return `${totalSec.toFixed(1)}s`;
  const mins = Math.floor(totalSec / 60);
  const secs = Math.floor(totalSec % 60);
  if (mins < 60) return `${mins}m ${String(secs).padStart(2, "0")}s`;
  const hours = Math.floor(mins / 60);
  const remMins = mins % 60;
  return `${hours}h ${String(remMins).padStart(2, "0")}m`;
}
