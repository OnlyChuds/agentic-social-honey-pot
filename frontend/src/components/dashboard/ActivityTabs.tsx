"use client";

import { useState } from "react";
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
  DecisionRecordJSON,
  EventJSON,
  FactJSON,
  RegisteredPageJSON,
  SignalPoint,
} from "@/lib/types";
import { cn, formatPercent, formatScore, formatTimeShort, getSuspicionBand } from "@/lib/utils";

interface ActivityTabsProps {
  runId: string;
  turns: ConversationTurnJSON[];
  events: EventJSON[];
  decisions: DecisionRecordJSON[];
  facts: FactJSON[];
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

  return [
    "<!doctype html><html><head><meta charset='utf-8'>",
    "<meta http-equiv='Content-Security-Policy' content=\"default-src 'self' data:; img-src 'self' data: http: https:; style-src 'unsafe-inline'; script-src 'none';\">",
    "<style>body{margin:0;padding:14px;background:#fafafa;color:#18181b;font-family:Inter,system-ui,-apple-system,sans-serif;font-size:13px;line-height:1.5;}img{max-width:100%;height:auto;}</style>",
    "</head><body>",
    sanitized,
    "</body></html>",
  ].join("");
}

export function ActivityTabs({
  runId,
  turns,
  events,
  decisions,
  facts,
  signals,
  registeredPages,
  assetsById,
  onSelectVersion,
}: ActivityTabsProps) {
  const [factFilter, setFactFilter] = useState("");
  const [sourceModeByTurn, setSourceModeByTurn] = useState<Record<number, boolean>>({});
  const [lightbox, setLightbox] = useState<LightboxImage | null>(null);
  const [expandedTranscript, setExpandedTranscript] = useState<string | null>(null);

  const eventsById = new Map(events.map((e) => [e.event_id, e]));
  const specialTargetEvents = events.filter(
    (e) =>
      e.type === "target_event" &&
      ["link.opened", "page.submitted", "attachment.opened"].includes(String(e.payload.event || ""))
  );

  const filteredFacts = facts.filter((f) => {
    const q = factFilter.trim().toLowerCase();
    if (!q) return true;
    return f.claim.toLowerCase().includes(q) || f.fact_id.toLowerCase().includes(q);
  });

  return (
    <>
      <Card className="border-zinc-800/80">
        <Tabs defaultValue="conversation">
          <CardHeader className="flex-row flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
            <TabsList className="h-8 bg-zinc-900/80">
              <TabsTrigger value="conversation" className="text-xs">
                Conversation ({turns.length})
              </TabsTrigger>
              <TabsTrigger value="decisions" className="text-xs">
                Decisions ({decisions.length})
              </TabsTrigger>
              <TabsTrigger value="facts" className="text-xs">
                Facts ({facts.length})
              </TabsTrigger>
            </TabsList>

            {registeredPages.length > 0 && (
              <span className="font-mono text-[11px] text-zinc-500">
                {registeredPages.length} registered page{registeredPages.length > 1 ? "s" : ""}
              </span>
            )}
          </CardHeader>

          <CardContent className="pt-4">
            {/* Conversation */}
            <TabsContent value="conversation" className="mt-0 space-y-3">
              {turns.length === 0 ? (
                <p className="py-6 text-center text-xs text-zinc-500">No messages yet.</p>
              ) : (
                <div className="space-y-3">
                  {turns.map((turn) => {
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

                    const isHtml = Boolean(turn.is_html);
                    const showSource = Boolean(sourceModeByTurn[turn.n]);

                    // Collect deduplicated visual image thumbnails (inline + attached images)
                    const imageItems: LightboxImage[] = [];
                    const seenImg = new Set<string>();
                    for (const img of turn.inline_images ?? []) {
                      if (img.exists && img.url && !seenImg.has(img.url)) {
                        seenImg.add(img.url);
                        imageItems.push({
                          url: img.url,
                          filename: img.filename,
                          assetId: img.asset_id,
                        });
                      }
                    }
                    const otherAttachments = (turn.attachments ?? []).filter((att) => {
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

                    return (
                      <div
                        key={`${turn.n}-${turn.event_ref}`}
                        onClick={() => version > 0 && onSelectVersion?.(version)}
                        className={cn(
                          "rounded-lg border px-4 py-3 transition-colors",
                          version > 0 && "cursor-pointer hover:border-zinc-700",
                          turn.dir === "out"
                            ? "border-zinc-800/80 bg-zinc-900/40"
                            : "border-zinc-800 bg-[#141418]"
                        )}
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-zinc-500">
                          <div className="flex items-center gap-2">
                            <span className="font-medium text-zinc-300">
                              {turn.dir === "out" ? "Outbound" : "Target"}
                            </span>
                            <span className="font-mono">{turn.channel}</span>
                            {subject && <span className="text-zinc-400">· {subject}</span>}
                          </div>
                          <div className="flex items-center gap-3 font-mono tabular-nums">
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
                                className="text-[11px] text-zinc-400 hover:text-zinc-100 cursor-pointer"
                              >
                                {showSource ? "Preview" : "Source"}
                              </button>
                            )}
                            {matchingSignal && suspBand && (
                              <span className={suspBand.textClass}>
                                susp {formatScore(matchingSignal.suspicion)}
                              </span>
                            )}
                            <span>{formatTimeShort(turn.ts)}</span>
                          </div>
                        </div>

                        {isHtml && !showSource ? (
                          <div
                            className="mt-2.5 overflow-hidden rounded-lg border border-zinc-800 bg-zinc-50"
                            onClick={(e) => e.stopPropagation()}
                          >
                            <iframe
                              title={`Email preview turn ${turn.n}`}
                              sandbox="allow-same-origin"
                              srcDoc={buildSafeEmailSrcDoc(turn.body, runId, assetsById)}
                              className="h-56 w-full border-0"
                            />
                          </div>
                        ) : (
                          <p
                            className={cn(
                              "mt-1.5 whitespace-pre-wrap text-xs leading-relaxed text-zinc-200",
                              isHtml && "font-mono text-[11px] text-zinc-400"
                            )}
                          >
                            {turn.body}
                          </p>
                        )}

                        {imageItems.length > 0 && (
                          <div
                            className="mt-2.5 flex flex-wrap items-center gap-2"
                            onClick={(e) => e.stopPropagation()}
                          >
                            {imageItems.map((img) => (
                              <button
                                key={`${turn.n}-${img.url}`}
                                type="button"
                                onClick={() => setLightbox(img)}
                                className="group flex items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-950/70 p-1.5 pr-2.5 text-left transition-colors hover:border-zinc-600 cursor-pointer"
                              >
                                {/* eslint-disable-next-line @next/next/no-img-element */}
                                <img
                                  src={img.url}
                                  alt={img.filename}
                                  className="h-10 w-16 rounded object-cover"
                                />
                                <div className="min-w-0">
                                  <p className="max-w-[160px] truncate text-[11px] text-zinc-200 group-hover:text-zinc-50">
                                    {img.filename}
                                  </p>
                                  <p className="font-mono text-[10px] text-zinc-500">
                                    {img.assetId}
                                  </p>
                                </div>
                              </button>
                            ))}
                          </div>
                        )}

                        {otherAttachments.length > 0 && (
                          <div
                            className="mt-2 flex flex-wrap items-center gap-2"
                            onClick={(e) => e.stopPropagation()}
                          >
                            {otherAttachments.map((att, idx) => {
                              const key = `${turn.n}-${att.filename}-${idx}`;
                              const isOpen = expandedTranscript === key;
                              return (
                                <div key={key} className="space-y-1">
                                  <div className="inline-flex items-center gap-2 rounded-md border border-zinc-800 bg-zinc-900/70 px-2.5 py-1 font-mono text-[11px] text-zinc-300">
                                    <span>{att.filename}</span>
                                    <span className="text-[10px] text-zinc-500">{att.kind}</span>
                                    {att.transcript && (
                                      <button
                                        type="button"
                                        onClick={() => setExpandedTranscript(isOpen ? null : key)}
                                        className="text-[10px] text-zinc-400 underline hover:text-zinc-100 cursor-pointer"
                                      >
                                        {isOpen ? "hide" : "transcript"}
                                      </button>
                                    )}
                                  </div>
                                  {isOpen && att.transcript && (
                                    <p className="rounded-md border border-zinc-800 bg-zinc-950/80 px-2.5 py-1.5 text-[11px] text-zinc-400">
                                      {att.transcript}
                                    </p>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    );
                  })}

                  {specialTargetEvents.map((ev) => (
                    <div
                      key={ev.event_id}
                      className="flex items-center justify-between rounded-lg border border-emerald-900/60 bg-emerald-950/20 px-4 py-2 font-mono text-xs text-emerald-300"
                    >
                      <span>{String(ev.payload.event)}</span>
                      <span className="text-[11px] text-emerald-400/70">
                        {formatTimeShort(ev.ts)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </TabsContent>

            {/* Decisions */}
            <TabsContent value="decisions" className="mt-0 divide-y divide-zinc-800/70">
              {decisions.length === 0 ? (
                <p className="py-6 text-center text-xs text-zinc-500">No decisions logged.</p>
              ) : (
                decisions.map((dec, idx) => (
                  <div
                    key={`${dec.ts}-${idx}`}
                    onClick={() => onSelectVersion?.(dec.snapshot_version)}
                    className="py-2.5 transition-colors hover:bg-zinc-900/40 cursor-pointer"
                  >
                    <div className="flex items-center justify-between gap-2 font-mono text-[11px] text-zinc-500">
                      <span>
                        v{dec.snapshot_version} · {dec.trigger} ·{" "}
                        {dec.actions.map((a) => a.op).join(", ") || "noop"}
                      </span>
                      <span>{formatTimeShort(dec.ts)}</span>
                    </div>
                    {dec.notes && <p className="mt-1 text-xs text-zinc-300">{dec.notes}</p>}
                  </div>
                ))
              )}
            </TabsContent>

            {/* Facts */}
            <TabsContent value="facts" className="mt-0 space-y-3">
              <Input
                value={factFilter}
                onChange={(e) => setFactFilter(e.target.value)}
                placeholder="Filter facts..."
                className="max-w-xs"
              />
              <div className="divide-y divide-zinc-800/70">
                {filteredFacts.map((f) => (
                  <div
                    key={f.fact_id}
                    className="flex items-baseline justify-between gap-4 py-2 text-xs"
                  >
                    <div className="min-w-0">
                      <span className="mr-2 font-mono text-[11px] text-zinc-500">{f.fact_id}</span>
                      <span
                        className={cn(
                          "text-zinc-300",
                          f.status === "superseded" && "text-zinc-500 line-through"
                        )}
                      >
                        {f.claim}
                      </span>
                    </div>
                    <span className="shrink-0 font-mono text-[11px] text-zinc-500">
                      {f.osint_grade ? `Grade ${f.osint_grade} · ` : ""}
                      {formatPercent(f.confidence)}
                    </span>
                  </div>
                ))}
              </div>
            </TabsContent>
          </CardContent>
        </Tabs>
      </Card>

      <Dialog open={lightbox !== null} onOpenChange={(open) => !open && setLightbox(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle className="text-sm">{lightbox?.filename}</DialogTitle>
            <DialogDescription className="font-mono text-[11px]">
              asset://{lightbox?.assetId}
            </DialogDescription>
          </DialogHeader>
          {lightbox && (
            <div className="flex items-center justify-center overflow-hidden rounded-lg border border-zinc-800 bg-zinc-950 p-3">
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
    </>
  );
}
