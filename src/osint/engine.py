"""Burst-based OSINT engine.

Fixed parallel research stages using native httpx (no bash/curl, no LLM in the loop):
  burst 1 - multi-provider seed searches, all in parallel (cap ~12s)
  burst 2 - LinkedIn scrape via Apify + top content-page reads, in parallel (cap ~38s)
  burst 3 - optional follow-up reads on URLs discovered in burst 2 (cap ~12s)

The agent makes exactly one LLM call afterwards to synthesize the dossier.
URL selection between stages is pure heuristics (see sources.pick_read_urls).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import Counter
from collections.abc import Awaitable
from dataclasses import dataclass, field

import httpx

from src.osint import providers
from src.osint.sources import Source, find_linkedin_url, host_of, pick_read_urls


@dataclass(slots=True)
class OsintResult:
    sources: list[Source] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    budget_usd: float = 0.0
    org_domain: str = ""
    total_s: float = 0.0


class OsintEngine:
    """Runs the burst pipeline against the configured providers."""

    def __init__(
        self,
        *,
        jina_key: str = "",
        exa_key: str = "",
        tavily_key: str = "",
        apify_token: str = "",
        max_budget_usd: float = 2.0,
        burst1_s: float = 12.0,
        burst2_s: float = 38.0,
        burst3_s: float = 12.0,
        read_limit_burst2: int = 3,
        read_limit_burst3: int = 2,
    ) -> None:
        self.jina_key = jina_key.strip()
        self.exa_key = exa_key.strip()
        self.tavily_key = tavily_key.strip()
        self.apify_token = apify_token.strip()
        self.max_budget_usd = max_budget_usd
        self.burst1_s = burst1_s
        self.burst2_s = burst2_s
        self.burst3_s = burst3_s
        self.read_limit_burst2 = read_limit_burst2
        self.read_limit_burst3 = read_limit_burst3

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        max_budget_usd: float = 2.0,
    ) -> OsintEngine:
        source = dict(os.environ) if env is None else dict(env)
        return cls(
            jina_key=source.get("JINA_API_KEY", ""),
            exa_key=source.get("EXA_API_KEY", ""),
            tavily_key=source.get("TAVILY_API_KEY", ""),
            apify_token=source.get("APIFY_API_TOKEN", ""),
            max_budget_usd=max_budget_usd,
        )

    def available_providers(self) -> list[str]:
        out: list[str] = []
        if self.jina_key:
            out.append("jina")
        if self.exa_key:
            out.append("exa")
        if self.tavily_key:
            out.append("tavily")
        if self.apify_token:
            out.append("apify")
        return out

    async def run(
        self,
        name: str,
        org: str,
        role: str = "",
        focus: str = "",
        *,
        client: httpx.AsyncClient | None = None,
    ) -> OsintResult:
        t_start = time.monotonic()
        result = OsintResult()
        owns_client = client is None
        http = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=6.0),
            headers={"User-Agent": "osint-swarm/1.0"},
        )
        try:
            name_org = f"{name} {org}".strip()
            focus_clean = focus.strip()
            b1_t0 = time.monotonic()
            b1_tasks: list[Awaitable[list[Source] | Source]] = []
            if self.jina_key:
                b1_tasks.append(providers.jina_search(http, self.jina_key, name_org))
                if focus_clean:
                    b1_tasks.append(
                        providers.jina_search(http, self.jina_key, f"{name_org} {focus_clean}")
                    )
                else:
                    b1_tasks.append(
                        providers.jina_search(
                            http,
                            self.jina_key,
                            f"{name_org} linkedin podcast interview speaker funding",
                        )
                    )
            if self.exa_key:
                b1_tasks.append(providers.exa_search(http, self.exa_key, name_org, category="personal site"))
                if focus_clean:
                    b1_tasks.append(
                        providers.exa_search(http, self.exa_key, f"{name_org} {focus_clean}")
                    )
                else:
                    b1_tasks.append(
                        providers.exa_search(
                            http,
                            self.exa_key,
                            f"{name_org} interview OR podcast OR keynote OR funding",
                        )
                    )
                b1_tasks.append(providers.exa_search(http, self.exa_key, org, category="company"))
            if self.tavily_key:
                tavily_q = f"{name_org} {focus_clean}".strip() if focus_clean else name_org
                b1_tasks.append(providers.tavily_search(http, self.tavily_key, tavily_q))
            burst1 = await self._stage(b1_tasks, cap_s=self.burst1_s, result=result)
            result.timings["burst1_s"] = time.monotonic() - b1_t0
            result.sources.extend(burst1)

            li_url = find_linkedin_url(burst1, name)
            read_urls = pick_read_urls(burst1, org=org, name=name, limit=self.read_limit_burst2)
            b2_t0 = time.monotonic()
            b2_tasks: list[Awaitable[list[Source] | Source]] = []
            if li_url and self.apify_token:
                b2_tasks.append(providers.apify_linkedin(http, self.apify_token, li_url))
            if self.jina_key:
                b2_tasks.extend(providers.jina_read(http, self.jina_key, u) for u in read_urls)
            burst2 = await self._stage(b2_tasks, cap_s=self.burst2_s, result=result)
            result.timings["burst2_s"] = time.monotonic() - b2_t0
            result.sources.extend(burst2)

            elapsed = time.monotonic() - t_start
            partial_budget = sum(s.cost_usd for s in result.sources)
            if (
                self.burst3_s > 0
                and self.jina_key
                and partial_budget < self.max_budget_usd
                and elapsed < self.burst1_s + 0.8 * self.burst2_s
            ):
                known = {s.url for s in result.sources if s.url}
                fresh = pick_read_urls(
                    burst2, org=org, name=name, exclude=known, limit=self.read_limit_burst3
                )
                if fresh:
                    b3_t0 = time.monotonic()
                    burst3 = await self._stage(
                        [providers.jina_read(http, self.jina_key, u) for u in fresh],
                        cap_s=self.burst3_s,
                        result=result,
                    )
                    result.timings["burst3_s"] = time.monotonic() - b3_t0
                    result.sources.extend(burst3)

            result.budget_usd = round(sum(s.cost_usd for s in result.sources), 4)
            result.org_domain = self._guess_org_domain(result.sources, org)
            result.timings["bursts_s"] = time.monotonic() - t_start
        finally:
            result.total_s = time.monotonic() - t_start
            if owns_client:
                await http.aclose()
        return result

    async def _stage(
        self,
        tasks: list[Awaitable[list[Source] | Source]],
        *,
        cap_s: float,
        result: OsintResult,
    ) -> list[Source]:
        if not tasks:
            return []
        wrapped = [asyncio.ensure_future(self._guard(t)) for t in tasks]
        _done, pending = await asyncio.wait(wrapped, timeout=cap_s)
        out: list[Source] = []
        for i, task in enumerate(wrapped):
            if task in pending:
                task.cancel()
                try:
                    await task
                except BaseException:  # noqa: BLE001, S110
                    # Swallow: task was cancelled at the stage deadline.
                    pass
                result.errors.append(f"stage task {i} timed out after {cap_s:.0f}s")
                continue
            try:
                out.extend(task.result())
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"stage task {i} failed: {exc}")
        return out

    @staticmethod
    async def _guard(coro: Awaitable[list[Source] | Source]) -> list[Source]:
        try:
            res = await coro
            return res if isinstance(res, list) else [res]
        except Exception as exc:  # noqa: BLE001
            return [Source("engine", "error", "provider error", ok=False, error=str(exc))]

    @staticmethod
    def _guess_org_domain(sources: list[Source], org: str) -> str:
        org_l = org.lower()
        counts: Counter[str] = Counter()
        for s in sources:
            for url in ([s.url] if s.url else []) + s.urls:
                host = host_of(url)
                if not host or "." not in host:
                    continue
                if any(host == j or host.endswith("." + j) for j in ("linkedin.com", "google.com", "gstatic.com", "schema.org", "facebook.com", "twitter.com", "x.com", "youtube.com", "instagram.com", "github.com")):
                    continue
                counts[host] += 3 if (org_l and org_l in host) else 1
        if not counts:
            return ""
        ranked = counts.most_common(8)
        if org_l:
            for host, _ in ranked:
                if org_l in host:
                    return host
        return ranked[0][0]


def render_findings(result: OsintResult, *, name: str, org: str, role: str, focus: str) -> str:
    """Compact text digest of all sources for the single LLM synthesis call."""
    header = f"TARGET: {name} | role: {role} | org: {org}"
    if focus:
        header += f" | focus: {focus}"
    lines = [
        header,
        f"ORG_DOMAIN_GUESS: {result.org_domain or 'unknown'}",
        f"TIMINGS: bursts={result.timings.get('bursts_s', 0.0):.1f}s budget=${result.budget_usd:.3f}",
        "",
    ]
    linkedin_sources = [s for s in result.sources if s.kind == "linkedin_scrape" and s.ok and s.text]
    read_sources = [s for s in result.sources if s.kind == "read" and (s.ok or s.text)]
    search_sources = [s for s in result.sources if s.kind == "search" and (s.ok or s.text)]

    for s in linkedin_sources:
        lines.append(f"## [linkedin scrape] {s.url} ({s.elapsed_s:.1f}s)")
        lines.append(s.text[:4500])

    for s in read_sources:
        lines.append(f"## [read] {s.url} ({s.elapsed_s:.1f}s) {s.title}".rstrip())
        lines.append(s.text[:2500])

    seen_search_urls: set[str] = set()
    last_label: str | None = None
    for s in search_sources:
        norm_url = s.url.rstrip("/").lower() if s.url else ""
        if norm_url:
            if norm_url in seen_search_urls:
                continue
            seen_search_urls.add(norm_url)
        if s.label != last_label:
            lines.append(f"## {s.label}")
            last_label = s.label
        entry = f"- {s.title} | {s.url}".strip(" |")
        if s.text:
            entry += f"\n  {s.text[:300]}"
        lines.append(entry)
    lines.append("")
    lines.append(
        "Provider errors (infra only, ignore unless no data): "
        + ("; ".join(result.errors[:6]) if result.errors else "none")
    )
    return "\n".join(lines)
