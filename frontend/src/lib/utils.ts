import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import type { CampaignState, DecisionActionJSON, TargetSummary } from "@/lib/types";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export interface SuspicionBand {
  label: "Low" | "Guarded" | "High" | "No replies yet";
  textClass: string;
  dotClass: string;
}

export function getSuspicionBand(value: number | null | undefined): SuspicionBand {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return {
      label: "No replies yet",
      textClass: "text-zinc-400",
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
    textClass: "text-zinc-300",
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
      return "text-zinc-200";
    default:
      return "text-zinc-400";
  }
}

export function formatStageState(stage: CampaignState): string {
  switch (stage) {
    case "BOOT":
      return "Boot";
    case "RESEARCH":
      return "Research";
    case "ENGAGE":
      return "Engage";
    case "WIN":
      return "Win";
    case "DONE":
      return "Done";
    case "ABORT":
      return "Aborted";
    default:
      return stage;
  }
}

export const STAGE_LABELS: Record<string, string> = {
  osint: "OSINT",
  identity: "Identity",
  profile: "Profile",
  strategy: "Strategy",
  messaging: "Messaging",
  qa: "QA Gate",
  target_api: "Target API",
};

export function formatAgentStage(stage: string): string {
  return STAGE_LABELS[stage] ?? titleCaseSnake(stage);
}

export function titleCaseSnake(raw: string | null | undefined): string {
  if (!raw) return "";
  const cleaned = raw.replaceAll("_", " ").replaceAll(".", " ").trim();
  if (!cleaned) return "";
  const titled = cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
  return titled
    .replace(/\bQa\b/g, "QA")
    .replace(/\bOsint\b/g, "OSINT")
    .replace(/\bApi\b/g, "API")
    .replace(/\bId\b/g, "ID");
}

export function formatTraitLabel(raw: string): string {
  const cleaned = raw.replaceAll("_", " ").trim();
  if (!cleaned) return raw;
  return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
}

export function formatChannel(raw: string | null | undefined): string {
  if (!raw) return "Message";
  const key = raw.toLowerCase();
  const map: Record<string, string> = {
    email: "Email",
    linkedin: "LinkedIn",
    linkedin_dm: "LinkedIn DM",
    linkedin_connection_request: "LinkedIn Request",
    linkedin_message: "LinkedIn",
    slack: "Slack",
    sms: "SMS",
    phone: "Phone",
    whatsapp: "WhatsApp",
  };
  return map[key] ?? titleCaseSnake(raw);
}

export function formatEventLabel(raw: string | null | undefined): string {
  if (!raw) return "Event";
  const map: Record<string, string> = {
    "link.opened": "Target opened link",
    "page.submitted": "Target submitted landing page",
    "attachment.opened": "Target opened attachment",
  };
  return map[raw] ?? titleCaseSnake(raw);
}

export function formatExitReason(raw: string | null | undefined, state: CampaignState): string {
  if (!raw) {
    if (state === "WIN") return "Objective reached";
    if (state === "DONE") return "Completed";
    if (state === "ABORT") return "Aborted";
    return "Ended";
  }
  const map: Record<string, string> = {
    win: "Objective reached",
    done: "Completed",
    completed: "Completed",
    budget_exhausted: "Touch budget exhausted",
    service_failure: "Stopped due to service error",
    aborted: "Aborted",
    stopped: "Stopped",
    timeout: "Timed out",
  };
  return map[raw.toLowerCase()] ?? titleCaseSnake(raw);
}

export function formatProposedNext(raw: string | null | undefined): string {
  if (!raw) return "";
  const map: Record<string, string> = {
    continue: "Continue plan",
    continue_nurture: "Continue nurturing",
    pivot: "Pivot strategy",
    pivot_angle: "Pivot angle",
    escalate: "Escalate lure",
    escalate_lure: "Escalate lure",
    back_off: "Back off",
    new_session: "Start new session",
    abort: "Abort campaign",
    wait: "Wait for response",
  };
  return map[raw.toLowerCase()] ?? titleCaseSnake(raw);
}

export function formatTrigger(raw: string | null | undefined): string {
  if (!raw) return "Step";
  const map: Record<string, string> = {
    boot: "Bootstrap",
    target_event: "Target activity",
    pacing: "Scheduled touch",
    manual: "Manual action",
    pivot: "Strategy pivot",
    new_session: "New session",
  };
  return map[raw.toLowerCase()] ?? titleCaseSnake(raw);
}

export function summarizeDecisionOps(actions: DecisionActionJSON[]): string {
  if (!actions || actions.length === 0) return "No action";
  const order: string[] = [];
  const counts = new Map<string, number>();
  for (const a of actions) {
    const op = a.op || "action";
    if (!counts.has(op)) order.push(op);
    counts.set(op, (counts.get(op) ?? 0) + 1);
  }
  const opNames: Record<string, string> = {
    commit: "Commit state",
    send: "Send message",
    trigger: "Run agent",
    register_page: "Register page",
    declare_incident: "Record win",
    new_session: "New session",
    reject: "Reject draft",
    stop: "Stop run",
  };
  return order
    .map((op) => {
      const label = opNames[op] ?? titleCaseSnake(op);
      const count = counts.get(op) ?? 1;
      return count > 1 ? `${label} ×${count}` : label;
    })
    .join(" · ");
}

export function formatDecisionNotes(raw: string | null | undefined): string {
  if (!raw) return "";
  return raw
    .replace(
      /Touch (\d+\/\d+) via ([a-z_]+);\s*events=(\d+)\.?/i,
      (_m, ratio: string, ch: string, evCount: string) => {
        const n = Number(evCount);
        const evText = n === 0 ? "no reply" : n === 1 ? "1 target event" : `${n} target events`;
        return `Touch ${ratio} via ${formatChannel(ch)} — ${evText}.`;
      }
    )
    .replace(/\bPlanned 1 touches\b/gi, "Planned 1 touch");
}

export function formatMarkerLabel(raw: string): { title: string; detail: string | null } {
  // Clean up verbose strategy pivot strings like:
  // "Strategy pivot: Planned 5 touches (objective=credentials, primary_hook=h_new_1) [pivot: 2 consecutive touches...]"
  const pivotMatch = raw.match(
    /^Strategy pivot:\s*Planned\s+(\d+)\s+touch(?:es)?\s*\(([^)]*)\)\s*(?:\[pivot:\s*(.+?)\])?$/i
  );
  if (pivotMatch) {
    const count = Number(pivotMatch[1]);
    const meta = pivotMatch[2] || "";
    const reason = pivotMatch[3]?.trim() || null;
    const objMatch = meta.match(/objective=([a-z_]+)/i);
    const objective = objMatch ? titleCaseSnake(objMatch[1]) : null;
    const touchWord = count === 1 ? "touch" : "touches";
    const title = objective
      ? `Strategy pivot — ${objective} (${count} ${touchWord})`
      : `Strategy pivot (${count} ${touchWord})`;
    return {
      title,
      detail: reason ? reason.charAt(0).toUpperCase() + reason.slice(1) : null,
    };
  }

  const pageMatch = raw.match(/^Registered page:\s*(.+)$/i);
  if (pageMatch) {
    return { title: `Registered landing page`, detail: pageMatch[1].trim() };
  }

  return {
    title: raw.replace(/\b1 touches\b/gi, "1 touch"),
    detail: null,
  };
}

export function formatMilestoneLabel(raw: string | null | undefined): string {
  if (!raw) return "";
  return raw
    .replace(/^Stage:\s*(BOOT|RESEARCH|ENGAGE|WIN|DONE|ABORT)$/i, (_m, st: string) =>
      `Stage: ${formatStageState(st.toUpperCase() as CampaignState)}`
    )
    .replace(/\b(BOOT|RESEARCH|ENGAGE|WIN|DONE|ABORT)\b/g, (st) =>
      formatStageState(st as CampaignState)
    );
}

export function formatRunTag(runId: string): string | null {
  const m = runId.match(/run[_-]?(\d+)$/i);
  if (m) return `Run ${m[1]}`;
  const tail = runId.split(/[_-]/).pop();
  if (tail && tail.length <= 8) return tail;
  return null;
}

export function isMockOrStubRun(item: TargetSummary): boolean {
  if (item.run_id.toLowerCase().includes("stub") || item.run_id.toLowerCase().includes("mock")) {
    return true;
  }
  if (item.sender_domain?.toLowerCase().endsWith(".test")) {
    return true;
  }
  if (
    item.total_elapsed_ms !== null &&
    item.total_elapsed_ms !== undefined &&
    item.total_elapsed_ms > 0 &&
    item.total_elapsed_ms < 500 &&
    item.touches_used > 0
  ) {
    return true;
  }
  return false;
}

export function formatDomainFromUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.hostname.replace(/^www\./, "");
  } catch {
    return null;
  }
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
