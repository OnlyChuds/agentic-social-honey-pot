"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import { useCallback, useMemo, useRef, useState } from "react";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type {
  AssetMetaJSON,
  ConversationTurnJSON,
  DecisionActionJSON,
  DecisionRecordJSON,
  EventJSON,
  FactJSON,
  RegisteredPageJSON,
  SignalPoint,
} from "@/lib/types";
import {
  cn,
  formatChannel,
  formatDecisionNotes,
  formatDomainFromUrl,
  formatEventLabel,
  formatPercent,
  formatProposedNext,
  formatScore,
  formatTimeShort,
  formatTrigger,
  getSuspicionBand,
  summarizeDecisionOps,
  titleCaseSnake,
} from "@/lib/utils";

interface ActivityTabsProps {
  runId: string;
  targetName?: string;
  senderName?: string;
  selectedVersion?: number;
  turns: ConversationTurnJSON[];
  events: EventJSON[];
  decisions: DecisionRecordJSON[];
  facts: FactJSON[];
  factsById?: Record<string, FactJSON>;
  signals: SignalPoint[];
  registeredPages: RegisteredPageJSON[];
  assetsById?: Record<string, AssetMetaJSON>;
  onSelectVersion?: (version: number) => void;
}

interface LightboxImage {
  url: string;
  filename: string;
  assetId: string;
}

const HTML_TAG_RE = /<(?:html|body|div|p|table|img|svg|br|h[1-6]|a)\b/i;
const FULL_DOC_RE = /<!doctype|<html\b/i;
const ASSET_URI_RE = /asset:\/\/([A-Za-z0-9_.-]+)/g;

function extractFallbackInlineImages(
  body: string,
  runId: string,
  assetsById?: Record<string, AssetMetaJSON>
): AssetMetaJSON[] {
  const out: AssetMetaJSON[] = [];
  const seen = new Set<string>();
  for (const match of body.matchAll(ASSET_URI_RE)) {
    const aid = match[1];
    if (!aid || seen.has(aid)) continue;
    seen.add(aid);
    const meta = assetsById?.[aid];
    out.push(
      meta ?? {
        asset_id: aid,
        filename: `${aid}.png`,
        content_type: "image/png",
        size_bytes: 0,
        url: `/api/targets/${encodeURIComponent(runId)}/assets/${encodeURIComponent(aid)}`,
        is_image: true,
        exists: true,
      }
    );
  }
  return out;
}

function extractFallbackAttachments(
  rawAttachments: unknown,
  runId: string,
  assetsById?: Record<string, AssetMetaJSON>
) {
  if (!Array.isArray(rawAttachments)) return [];
  return rawAttachments
    .map((item) => {
      if (typeof item === "string") {
        const aid = item.replace(/^asset:\/\//, "");
        const meta = assetsById?.[aid];
        return {
          kind: (meta ? (meta.is_image ? "image" : "file") : "image") as
            | "image"
            | "file"
            | "virtual",
          asset_id: aid,
          url:
            meta?.url ??
            `/api/targets/${encodeURIComponent(runId)}/assets/${encodeURIComponent(aid)}`,
          filename: meta?.filename ?? `${aid}.png`,
          content_type: meta?.content_type ?? "image/png",
          exists: meta?.exists ?? true,
          transcript: null,
        };
      }
      if (item && typeof item === "object") {
        const obj = item as Record<string, unknown>;
        return {
          kind: "virtual" as const,
          asset_id: null,
          url: null,
          filename: String(obj.filename ?? obj.name ?? "attachment"),
          content_type: null,
          exists: true,
          transcript: typeof obj.transcript === "string" ? obj.transcript : null,
        };
      }
      return null;
    })
    .filter((v): v is NonNullable<typeof v> => v !== null);
}

function buildSafeEmailSrcDoc(
  rawHtml: string,
  runId: string,
  assetsById?: Record<string, AssetMetaJSON>
): string {
  const sanitized = rawHtml
    .replace(/<script\b[\s\S]*?<\/script>/gi, "")
    .replace(/\son[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, "")
    .replace(/asset:\/\/([A-Za-z0-9_.-]+)/g, (_match, assetId: string) => {
      const meta = assetsById?.[assetId];
      if (meta?.url) return meta.url;
      return `/api/targets/${encodeURIComponent(runId)}/assets/${encodeURIComponent(assetId)}`;
    });

  const isFullDoc = FULL_DOC_RE.test(sanitized);
  const injectedStyles = [
    "<meta http-equiv='Content-Security-Policy' content=\"default-src 'self' data:; img-src 'self' data: http: https:; style-src 'unsafe-inline'; script-src 'none';\">",
    "<style>",
    "html,body{margin:0;overflow-x:hidden;scrollbar-width:thin;scrollbar-color:#a1a1aa transparent;}",
    "::-webkit-scrollbar{width:5px;height:5px;}::-webkit-scrollbar-thumb{background:#a1a1aa;border-radius:9999px;}",
    isFullDoc
      ? "body{background:#f3f4f6 !important;padding:14px 12px !important;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif !important;}img{max-width:100%;height:auto;}"
      : "body{padding:18px 20px;background:#f4f4f5;color:#18181b;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:13px;line-height:1.55;}img{max-width:100%;height:auto;}",
    "</style>",
  ].join("");

  if (isFullDoc && /<head\b[^>]*>/i.test(sanitized)) {
    return sanitized.replace(/<head\b[^>]*>/i, (m) => `${m}${injectedStyles}`);
  }

  return [
    "<!doctype html><html><head><meta charset='utf-8'>",
    injectedStyles,
    "</head><body>",
    sanitized,
    "</body></html>",
  ].join("");
}

function EmailPreviewFrame({
  title,
  srcDoc,
}: {
  title: string;
  srcDoc: string;
}) {
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const [contentHeight, setContentHeight] = useState<number>(280);
  const [expanded, setExpanded] = useState(false);

  const measure = useCallback(() => {
    const doc = iframeRef.current?.contentDocument;
    if (!doc) return;
    const h = Math.max(
      doc.body?.scrollHeight ?? 0,
      doc.documentElement?.scrollHeight ?? 0
    );
    if (h > 60) {
      setContentHeight(Math.min(h + 8, 900));
    }
  }, []);

  const isTall = contentHeight > 340;
  const visibleHeight = expanded ? contentHeight : Math.min(contentHeight, 340);

  return (
    <div
      className="mt-3 overflow-hidden rounded-lg border border-zinc-800/90 bg-zinc-950/70"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="relative overflow-hidden bg-[#f3f4f6]">
        <iframe
          ref={iframeRef}
          title={title}
          sandbox="allow-same-origin"
          srcDoc={srcDoc}
          onLoad={measure}
          style={{ height: `${visibleHeight}px` }}
          className="block w-full border-0 transition-[height] duration-150"
        />
        {isTall && !expanded && (
          <div className="pointer-events-none absolute inset-x-0 bottom-0 h-12 bg-gradient-to-t from-[#f3f4f6] to-transparent" />
        )}
      </div>
      {isTall && (
        <div className="flex justify-center border-t border-zinc-800/80 bg-card px-3 py-1.5">
          <button
            type="button"
            onClick={() => setExpanded((prev) => !prev)}
            className="cursor-pointer text-xs text-zinc-400 transition-colors hover:text-zinc-100"
          >
            {expanded ? "Show less" : "Show full email"}
          </button>
        </div>
      )}
    </div>
  );
}

function formatActionPayload(action: DecisionActionJSON): string | null {
  const entries = Object.entries(action).filter(([k, v]) => {
    if (k === "op" || v === null || v === undefined || v === "") return false;
    return true;
  });
  if (entries.length === 0) return null;
  return entries
    .map(([k, v]) => {
      const val =
        typeof v === "string"
          ? v.length > 90
            ? `${v.slice(0, 87)}…`
            : v
          : Array.isArray(v)
            ? `${v.length} items`
            : typeof v === "object"
              ? JSON.stringify(v).slice(0, 80)
              : String(v);
      return `${titleCaseSnake(k)}: ${val}`;
    })
    .join(" · ");
}

type TimelineItem =
  | { kind: "turn"; ts: string; order: number; turn: ConversationTurnJSON }
  | { kind: "event"; ts: string; order: number; event: EventJSON };

export function ActivityTabs({
  runId,
  targetName,
  senderName,
  selectedVersion,
  turns,
  events,
  decisions,
  facts,
  factsById = {},
  signals,
  registeredPages,
  assetsById,
  onSelectVersion,
}: ActivityTabsProps) {
  const [factFilter, setFactFilter] = useState("");
  const [sourceModeByTurn, setSourceModeByTurn] = useState<Record<number, boolean>>({});
  const [reasoningOpenByTurn, setReasoningOpenByTurn] = useState<Record<number, boolean>>({});
  const [expandedDecisionIdx, setExpandedDecisionIdx] = useState<number | null>(null);
  const [lightbox, setLightbox] = useState<LightboxImage | null>(null);
  const [pagesModalOpen, setPagesModalOpen] = useState(false);
  const [expandedTranscript, setExpandedTranscript] = useState<string | null>(null);

  const eventsById = useMemo(
    () => new Map(events.map((e) => [e.event_id, e])),
    [events]
  );

  const specialTargetEvents = useMemo(
    () =>
      events.filter(
        (e) =>
          e.type === "target_event" &&
          ["link.opened", "page.submitted", "attachment.opened"].includes(
            String(e.payload.event || "")
          )
      ),
    [events]
  );

  const chronologicalFeed = useMemo<TimelineItem[]>(() => {
    const items: TimelineItem[] = [
      ...turns.map((turn, idx) => ({
        kind: "turn" as const,
        ts: turn.ts || "",
        order: idx,
        turn,
      })),
      ...specialTargetEvents.map((event, idx) => ({
        kind: "event" as const,
        ts: event.ts || "",
        order: turns.length + idx,
        event,
      })),
    ];
    return items.sort((a, b) => {
      const cmp = a.ts.localeCompare(b.ts);
      return cmp !== 0 ? cmp : a.order - b.order;
    });
  }, [turns, specialTargetEvents]);

  const agentErrors = useMemo(
    () => events.filter((e) => e.type === "system" && e.payload.kind === "agent_error"),
    [events]
  );

  const filteredFacts = useMemo(() => {
    const q = factFilter.trim().toLowerCase();
    if (!q) return facts;
    return facts.filter(
      (f) =>
        f.claim.toLowerCase().includes(q) ||
        f.fact_id.toLowerCase().includes(q) ||
        f.category.toLowerCase().includes(q)
    );
  }, [facts, factFilter]);

  return (
    <>
      <Card>
        <Tabs defaultValue="conversation">
          <CardHeader className="flex-row items-center justify-between space-y-0 border-b border-zinc-800/80 pb-0">
            <TabsList variant="line">
              <TabsTrigger value="conversation">
                Conversation
                <span className="font-mono text-xs tabular-nums text-zinc-400">
                  {turns.length}
                </span>
              </TabsTrigger>
              <TabsTrigger value="decisions">
                Agent Decisions
                <span className="font-mono text-xs tabular-nums text-zinc-400">
                  {decisions.length}
                </span>
              </TabsTrigger>
              <TabsTrigger value="facts">
                Facts &amp; OSINT
                <span className="font-mono text-xs tabular-nums text-zinc-400">
                  {facts.length}
                </span>
              </TabsTrigger>
            </TabsList>

            {registeredPages.length > 0 && (
              <button
                type="button"
                onClick={() => setPagesModalOpen(true)}
                className="mb-3 cursor-pointer text-xs text-zinc-400 underline decoration-zinc-700 underline-offset-4 transition-colors hover:text-zinc-100"
              >
                {registeredPages.length} registered{" "}
                {registeredPages.length === 1 ? "landing page" : "landing pages"}
              </button>
            )}
          </CardHeader>

          <CardContent className="pt-5">
            {/* Conversation & chronological target events */}
            <TabsContent value="conversation" className="mt-0 space-y-3">
              {chronologicalFeed.length === 0 ? (
                <p className="py-8 text-center text-xs text-zinc-400">No messages sent yet.</p>
              ) : (
                <div className="space-y-3">
                  {chronologicalFeed.map((item) => {
                    if (item.kind === "event") {
                      const ev = item.event;
                      return (
                        <button
                          key={ev.event_id}
                          type="button"
                          onClick={() => ev.base_version > 0 && onSelectVersion?.(ev.base_version)}
                          className="flex w-full cursor-pointer items-center gap-3 py-1 text-xs text-emerald-400 transition-opacity hover:opacity-90"
                        >
                          <span className="h-px flex-1 bg-emerald-900/60" />
                          <span className="font-medium">
                            {formatEventLabel(String(ev.payload.event))}
                          </span>
                          <span className="font-mono text-[11px] tabular-nums text-emerald-400/75">
                            {formatTimeShort(ev.ts)}
                          </span>
                          <span className="h-px flex-1 bg-emerald-900/60" />
                        </button>
                      );
                    }

                    const turn = item.turn;
                    const ev = eventsById.get(turn.event_ref);
                    const version = ev?.base_version ?? 0;
                    const subject =
                      turn.subject ??
                      (typeof ev?.payload?.subject === "string" ? ev.payload.subject : null);
                    const matchingSignal =
                      turn.dir === "in"
                        ? signals.find((s) => Math.abs(s.version - version) <= 2)
                        : undefined;
                    const suspBand = matchingSignal
                      ? getSuspicionBand(matchingSignal.suspicion)
                      : null;

                    const isHtml =
                      turn.is_html !== undefined
                        ? Boolean(turn.is_html)
                        : HTML_TAG_RE.test(turn.body);
                    const showSource = Boolean(sourceModeByTurn[turn.n]);
                    const showReasoning = Boolean(reasoningOpenByTurn[turn.n]);
                    const isSelectedVersion =
                      selectedVersion !== undefined &&
                      version > 0 &&
                      Math.abs(version - selectedVersion) <= 1;

                    const factsUsedIds = turn.facts_used ?? [];
                    const hasReasoning =
                      factsUsedIds.length > 0 ||
                      Boolean(matchingSignal?.reason) ||
                      Boolean(matchingSignal?.proposed_next);

                    const resolvedInlineImages =
                      turn.inline_images !== undefined
                        ? turn.inline_images
                        : extractFallbackInlineImages(turn.body, runId, assetsById);
                    const resolvedAttachments =
                      turn.attachments !== undefined
                        ? turn.attachments
                        : extractFallbackAttachments(ev?.payload?.attachments, runId, assetsById);

                    const imageItems: LightboxImage[] = [];
                    const seenImg = new Set<string>();
                    for (const img of resolvedInlineImages) {
                      if (img.exists && img.url && !seenImg.has(img.url)) {
                        seenImg.add(img.url);
                        imageItems.push({
                          url: img.url,
                          filename: img.filename,
                          assetId: img.asset_id,
                        });
                      }
                    }
                    const otherAttachments = resolvedAttachments.filter((att) => {
                      if (att.kind === "image" && att.exists && att.url) {
                        if (!seenImg.has(att.url)) {
                          seenImg.add(att.url);
                          imageItems.push({
                            url: att.url,
                            filename: att.filename,
                            assetId: att.asset_id ?? att.filename,
                          });
                        }
                        return false;
                      }
                      return true;
                    });

                    const speakerLabel =
                      turn.dir === "out"
                        ? senderName || "AI Persona"
                        : targetName || "Target";

                    return (
                      <article
                        key={`${turn.n}-${turn.event_ref}`}
                        onClick={() => version > 0 && onSelectVersion?.(version)}
                        className={cn(
                          "rounded-lg border border-l-2 px-4 py-3.5 transition-colors",
                          version > 0 && "cursor-pointer",
                          turn.dir === "out"
                            ? "border-zinc-800/80 border-l-zinc-500 bg-background/40 hover:border-zinc-700"
                            : "border-zinc-800 border-l-zinc-200 bg-card-elevated/50 hover:border-zinc-700",
                          isSelectedVersion && "border-zinc-600"
                        )}
                      >
                        <div className="flex items-baseline justify-between gap-4 text-xs">
                          <div className="flex min-w-0 items-baseline gap-2">
                            <span className="shrink-0 whitespace-nowrap font-medium text-zinc-100">
                              {speakerLabel}
                            </span>
                            <span className="shrink-0 whitespace-nowrap text-zinc-400">
                              {turn.dir === "out" ? "sent" : "replied via"}{" "}
                              {formatChannel(turn.channel)}
                            </span>
                            {subject && (
                              <span className="truncate text-zinc-300">· {subject}</span>
                            )}
                          </div>

                          <div className="flex shrink-0 items-baseline gap-3.5 text-xs">
                            {hasReasoning && (
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setReasoningOpenByTurn((prev) => ({
                                    ...prev,
                                    [turn.n]: !prev[turn.n],
                                  }));
                                }}
                                className="cursor-pointer text-zinc-400 transition-colors hover:text-zinc-100"
                              >
                                {showReasoning
                                  ? "Hide reasoning"
                                  : factsUsedIds.length > 0
                                    ? `${factsUsedIds.length} ${factsUsedIds.length === 1 ? "fact" : "facts"} used`
                                    : "Agent assessment"}
                              </button>
                            )}
                            {isHtml && (
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setSourceModeByTurn((prev) => ({
                                    ...prev,
                                    [turn.n]: !prev[turn.n],
                                  }));
                                }}
                                className="cursor-pointer text-zinc-400 transition-colors hover:text-zinc-100"
                              >
                                {showSource ? "Preview" : "HTML"}
                              </button>
                            )}
                            {matchingSignal && suspBand && (
                              <span className={cn("font-mono tabular-nums", suspBand.textClass)}>
                                {formatScore(matchingSignal.suspicion)}{" "}
                                <span className="font-sans text-zinc-400">suspicion</span>
                              </span>
                            )}
                            <span className="font-mono tabular-nums text-zinc-400">
                              {formatTimeShort(turn.ts)}
                            </span>
                          </div>
                        </div>

                        {isHtml && !showSource ? (
                          <EmailPreviewFrame
                            title={`Email preview turn ${turn.n}`}
                            srcDoc={buildSafeEmailSrcDoc(turn.body, runId, assetsById)}
                          />
                        ) : (
                          <p
                            className={cn(
                              "mt-2 whitespace-pre-wrap leading-relaxed",
                              isHtml
                                ? "overflow-x-auto rounded-lg border border-zinc-800 bg-background/80 p-3 font-mono text-[11px] text-zinc-400"
                                : "text-[13.5px] text-zinc-200"
                            )}
                          >
                            {turn.body}
                          </p>
                        )}

                        {/* On-demand agent reasoning drawer for this turn */}
                        {showReasoning && hasReasoning && (
                          <div
                            className="mt-3 space-y-2 border-l border-zinc-700 pl-3 text-xs"
                            onClick={(e) => e.stopPropagation()}
                          >
                            {matchingSignal && (
                              <div className="space-y-1">
                                <p className="text-zinc-400">
                                  Assessment · Mood:{" "}
                                  <span className="text-zinc-200">
                                    {matchingSignal.mood || "Neutral"}
                                  </span>
                                  {matchingSignal.proposed_next && (
                                    <>
                                      {" · "}Next move:{" "}
                                      <span className="text-zinc-200">
                                        {formatProposedNext(matchingSignal.proposed_next)}
                                      </span>
                                    </>
                                  )}
                                </p>
                                {matchingSignal.reason && (
                                  <p className="leading-relaxed text-zinc-300">
                                    {matchingSignal.reason}
                                  </p>
                                )}
                              </div>
                            )}

                            {factsUsedIds.length > 0 && (
                              <div className="space-y-1">
                                <p className="text-zinc-400">Facts referenced in this message</p>
                                <ul className="space-y-1">
                                  {factsUsedIds.map((fid) => {
                                    const f = factsById[fid];
                                    return (
                                      <li key={fid} className="text-zinc-300">
                                        {f ? f.claim : fid}
                                      </li>
                                    );
                                  })}
                                </ul>
                              </div>
                            )}
                          </div>
                        )}

                        {imageItems.length > 0 && (
                          <div
                            className="mt-3 flex flex-wrap items-center gap-2.5"
                            onClick={(e) => e.stopPropagation()}
                          >
                            {imageItems.map((img) => (
                              <button
                                key={`${turn.n}-${img.url}`}
                                type="button"
                                onClick={() => setLightbox(img)}
                                className="group flex cursor-pointer items-center gap-2.5 rounded-lg border border-zinc-800 bg-background/70 p-1.5 pr-3 text-left transition-colors hover:border-zinc-700"
                              >
                                {/* eslint-disable-next-line @next/next/no-img-element */}
                                <img
                                  src={img.url}
                                  alt={img.filename}
                                  className="h-9 w-9 rounded object-cover object-top"
                                />
                                <span className="max-w-[240px] truncate text-xs text-zinc-300 group-hover:text-zinc-100">
                                  {img.filename}
                                </span>
                              </button>
                            ))}
                          </div>
                        )}

                        {otherAttachments.length > 0 && (
                          <div
                            className="mt-2.5 flex flex-wrap items-center gap-2.5"
                            onClick={(e) => e.stopPropagation()}
                          >
                            {otherAttachments.map((att, idx) => {
                              const key = `${turn.n}-${att.filename}-${idx}`;
                              const isOpen = expandedTranscript === key;
                              return (
                                <div key={key} className="w-full space-y-1.5">
                                  <div className="inline-flex items-center gap-2.5 rounded-md border border-zinc-800 bg-background/70 px-3 py-1.5 text-xs text-zinc-300">
                                    <span>{att.filename}</span>
                                    {att.transcript && (
                                      <button
                                        type="button"
                                        onClick={() =>
                                          setExpandedTranscript(isOpen ? null : key)
                                        }
                                        className="cursor-pointer text-zinc-400 underline decoration-zinc-600 underline-offset-2 hover:text-zinc-100"
                                      >
                                        {isOpen ? "Hide transcript" : "Read transcript"}
                                      </button>
                                    )}
                                  </div>
                                  {isOpen && att.transcript && (
                                    <p className="rounded-lg border border-zinc-800 bg-background/80 px-3.5 py-2.5 text-xs leading-relaxed text-zinc-300">
                                      {att.transcript}
                                    </p>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </article>
                    );
                  })}
                </div>
              )}
            </TabsContent>

            {/* Orchestrator Decisions */}
            <TabsContent value="decisions" className="mt-0 space-y-3">
              {agentErrors.length > 0 && (
                <div className="space-y-1.5 rounded-lg border border-rose-900/50 bg-rose-950/20 p-3.5 text-xs">
                  <p className="font-medium text-rose-300">
                    System warnings ({agentErrors.length})
                  </p>
                  {agentErrors.map((ev, eIdx) => {
                    const rawErr = String(ev.payload.error ?? "Unknown error");
                    const conciseErr = rawErr.split("\n")[0].replace(/^(ValueError|RuntimeError|TypeError):\s*/i, "");
                    const trimmedErr =
                      conciseErr.length > 140 ? `${conciseErr.slice(0, 137)}…` : conciseErr;
                    return (
                      <div
                        key={`${ev.event_id}-${eIdx}`}
                        className="flex items-baseline justify-between gap-3"
                      >
                        <span className="text-rose-200/90" title={rawErr}>
                          {titleCaseSnake(String(ev.payload.failed_actor ?? ev.actor))}: {trimmedErr}
                        </span>
                        <span className="shrink-0 font-mono text-[11px] tabular-nums text-rose-400/75">
                          {formatTimeShort(ev.ts)}
                        </span>
                      </div>
                    );
                  })}
                </div>
              )}

              <div className="divide-y divide-zinc-800/70">
                {decisions.length === 0 ? (
                  <p className="py-8 text-center text-xs text-zinc-400">
                    No decisions logged yet.
                  </p>
                ) : (
                  decisions.map((dec, idx) => {
                    const actionDetails = dec.actions
                      .map((a) => ({ op: a.op, detail: formatActionPayload(a) }))
                      .filter((x) => x.detail !== null);
                    const isOpen = expandedDecisionIdx === idx;

                    return (
                      <div key={`${dec.ts}-${idx}`} className="py-3">
                        <div className="flex items-baseline justify-between gap-4">
                          <button
                            type="button"
                            onClick={() => onSelectVersion?.(dec.snapshot_version)}
                            className="flex cursor-pointer items-baseline gap-2 text-left text-xs transition-colors hover:text-zinc-50"
                          >
                            <span className="font-medium text-zinc-200">
                              {formatTrigger(dec.trigger)}
                            </span>
                            <span className="text-zinc-400">
                              · {summarizeDecisionOps(dec.actions)}
                            </span>
                          </button>

                          <div className="flex shrink-0 items-center gap-3 text-xs">
                            {actionDetails.length > 0 && (
                              <button
                                type="button"
                                onClick={() => setExpandedDecisionIdx(isOpen ? null : idx)}
                                className="inline-flex cursor-pointer items-center gap-1 text-zinc-400 transition-colors hover:text-zinc-100"
                              >
                                {isOpen ? (
                                  <ChevronDown className="h-3 w-3" />
                                ) : (
                                  <ChevronRight className="h-3 w-3" />
                                )}
                                Details
                              </button>
                            )}
                            <button
                              type="button"
                              onClick={() => onSelectVersion?.(dec.snapshot_version)}
                              className="cursor-pointer font-mono tabular-nums text-zinc-400 hover:text-zinc-100"
                            >
                              v{dec.snapshot_version}
                            </button>
                            <span className="font-mono tabular-nums text-zinc-400">
                              {formatTimeShort(dec.ts)}
                            </span>
                          </div>
                        </div>

                        {dec.notes && (
                          <p className="mt-1 text-[13px] leading-relaxed text-zinc-300">
                            {formatDecisionNotes(dec.notes)}
                          </p>
                        )}

                        {isOpen && actionDetails.length > 0 && (
                          <div className="mt-2 space-y-1 border-l border-zinc-800 pl-3 text-xs text-zinc-400">
                            {actionDetails.map((a, aIdx) => (
                              <p key={aIdx} className="leading-relaxed">
                                <span className="text-zinc-300">{titleCaseSnake(a.op)}</span> —{" "}
                                {a.detail}
                              </p>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })
                )}
              </div>
            </TabsContent>

            {/* Facts & OSINT */}
            <TabsContent value="facts" className="mt-0 space-y-3">
              <Input
                value={factFilter}
                onChange={(e) => setFactFilter(e.target.value)}
                placeholder="Filter facts..."
                aria-label="Filter facts"
                className="max-w-xs"
              />
              {filteredFacts.length === 0 ? (
                <p className="py-8 text-center text-xs text-zinc-400">
                  {factFilter.trim() ? "No facts match your filter." : "No facts collected yet."}
                </p>
              ) : (
                <div className="divide-y divide-zinc-800/70">
                  {filteredFacts.map((f, fIdx) => {
                    const domain = formatDomainFromUrl(f.source);
                    return (
                      <div
                        key={`${f.fact_id}-${fIdx}`}
                        className="flex items-baseline justify-between gap-6 py-2.5"
                      >
                        <div className="min-w-0 space-y-0.5">
                          <p
                            className={cn(
                              "text-[13px] leading-snug",
                              f.status === "superseded"
                                ? "text-zinc-500 line-through"
                                : "text-zinc-200"
                            )}
                          >
                            {f.claim}
                          </p>
                          <p className="text-xs text-zinc-400">
                            {f.provenance === "osint"
                              ? `OSINT${f.osint_grade ? ` · Grade ${f.osint_grade}` : ""}`
                              : f.provenance === "target_claim"
                                ? "Target statement"
                                : "Inferred"}
                            {domain && (
                              <>
                                {" · "}
                                <a
                                  href={f.source}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="underline decoration-zinc-700 underline-offset-2 hover:text-zinc-200"
                                >
                                  {domain}
                                </a>
                              </>
                            )}
                            {f.status === "superseded" && " · Superseded"}
                          </p>
                        </div>
                        <span className="shrink-0 font-mono text-xs tabular-nums text-zinc-400">
                          {formatPercent(f.confidence)}
                        </span>
                      </div>
                    );
                  })}
                </div>
              )}
            </TabsContent>
          </CardContent>
        </Tabs>
      </Card>

      {/* Asset Lightbox */}
      <Dialog open={lightbox !== null} onOpenChange={(open) => !open && setLightbox(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle className="text-sm">{lightbox?.filename}</DialogTitle>
            <DialogDescription className="font-mono text-xs">
              asset://{lightbox?.assetId}
            </DialogDescription>
          </DialogHeader>
          {lightbox && (
            <div className="flex items-center justify-center overflow-hidden rounded-lg border border-zinc-800 bg-background p-3">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={lightbox.url}
                alt={lightbox.filename}
                className="max-h-[65vh] w-auto rounded object-contain"
              />
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* Registered Landing Pages Modal */}
      <Dialog open={pagesModalOpen} onOpenChange={setPagesModalOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle className="text-sm">Registered Landing Pages</DialogTitle>
            <DialogDescription>
              Simulated lure pages provisioned by the campaign agent for this target.
            </DialogDescription>
          </DialogHeader>
          <div className="divide-y divide-zinc-800/80">
            {registeredPages.map((p) => (
              <div key={p.page_id} className="space-y-1.5 py-3 first:pt-1 last:pb-1">
                <div className="flex items-baseline justify-between gap-3">
                  <p className="text-sm font-medium text-zinc-100">{p.title || "Landing page"}</p>
                  <span className="shrink-0 font-mono text-xs tabular-nums text-zinc-400">
                    {formatTimeShort(p.created_at)}
                  </span>
                </div>
                <p className="break-all font-mono text-xs text-zinc-400">{p.url}</p>
                {p.fields.length > 0 && (
                  <p className="text-xs text-zinc-300">
                    Fields captured: {p.fields.map((f) => titleCaseSnake(f)).join(", ")}
                  </p>
                )}
              </div>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
