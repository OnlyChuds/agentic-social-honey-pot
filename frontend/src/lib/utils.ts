import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

export function formatScore(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "—";
  }
  return value.toFixed(2);
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "—";
  }
  return `${Math.round(value * 100)}%`;
}

export function formatTimeShort(ts: string | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) {
    return ts.slice(11, 19) || ts;
  }
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function formatTraitLabel(trait: string): string {
  const cleaned = trait.replace(/[_-]+/g, " ").trim();
  if (!cleaned) return trait;
  return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
}

export function getStageTextClass(stage: string): string {
  switch (stage) {
    case "WIN":
      return "text-emerald-400";
    case "ABORT":
      return "text-rose-400";
    case "ENGAGE":
      return "text-sky-400";
    case "RESEARCH":
      return "text-amber-400";
    case "DONE":
      return "text-zinc-400";
    default:
      return "text-zinc-300";
  }
}

export function getSuspicionBand(value: number | null | undefined): {
  label: string;
  textClass: string;
} {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return { label: "No signal", textClass: "text-zinc-500" };
  }
  if (value >= 0.6) {
    return { label: "High", textClass: "text-rose-400" };
  }
  if (value >= 0.3) {
    return { label: "Elevated", textClass: "text-amber-400" };
  }
  return { label: "Low", textClass: "text-emerald-400" };
}
