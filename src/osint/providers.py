"""Async httpx clients for OSINT providers.

Replaces the vendor bash/curl script chain (bash -> curl -> python3) with direct
HTTP calls: no process spawn, connection reuse, tight timeouts, never raises.
Endpoints mirror vendor/osint-skill (jina s./r., exa search, tavily search, apify runs).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

import httpx

from src.osint.sources import Source, extract_urls

APIFY_BASE = "https://api.apify.com/v2"
LINKEDIN_ACTOR = "supreme_coder~linkedin-profile-scraper"


def _jina_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        inner = data.get("data", data)
        if isinstance(inner, dict):
            items = inner.get("results") or inner.get("items") or []
            return [i for i in items if isinstance(i, dict)]
        if isinstance(inner, list):
            return [i for i in inner if isinstance(i, dict)]
    if isinstance(data, list):
        return [i for i in data if isinstance(i, dict)]
    return []


async def jina_search(client: httpx.AsyncClient, api_key: str, query: str) -> list[Source]:
    t0 = time.monotonic()
    label = f'jina_search "{query}"'
    cost = 0.001
    try:
        r = await client.get(
            f"https://s.jina.ai/{quote(query, safe='')}",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        data: Any = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        return [Source("jina", "search", label, ok=False, error=str(exc), elapsed_s=time.monotonic() - t0, cost_usd=cost)]
    elapsed = time.monotonic() - t0
    out: list[Source] = []
    for i, item in enumerate(_jina_items(data)[:10]):
        url = str(item.get("url") or "")
        title = str(item.get("title") or "")
        text = str(item.get("content") or item.get("description") or "")[:400]
        out.append(
            Source(
                "jina",
                "search",
                label,
                url=url,
                title=title,
                text=text,
                urls=[url] if url else extract_urls(text, 5),
                elapsed_s=elapsed,
                cost_usd=cost if i == 0 else 0.0,
            )
        )
    if not out:
        out.append(Source("jina", "search", label, elapsed_s=elapsed, cost_usd=cost))
    return out


async def jina_read(client: httpx.AsyncClient, api_key: str, url: str) -> Source:
    t0 = time.monotonic()
    label = f"jina_read {url}"
    cost = 0.001
    try:
        r = await client.get(
            f"https://r.jina.ai/{url}",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        data: Any = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        return Source("jina", "read", label, url=url, ok=False, error=str(exc), elapsed_s=time.monotonic() - t0, cost_usd=cost)
    inner = data.get("data", data) if isinstance(data, dict) else {}
    if not isinstance(inner, dict):
        inner = {}
    title = str(inner.get("title") or "")
    text = str(inner.get("content") or "")[:12000]
    return Source(
        "jina",
        "read",
        label,
        url=url,
        title=title,
        text=text,
        urls=extract_urls(text, 15),
        elapsed_s=time.monotonic() - t0,
        cost_usd=cost,
    )


async def exa_search(
    client: httpx.AsyncClient,
    api_key: str,
    query: str,
    *,
    category: str | None = None,
    n: int = 10,
    max_chars: int = 500,
) -> list[Source]:
    t0 = time.monotonic()
    label = f'exa_search "{query}"'
    cost = 0.005
    body: dict[str, Any] = {
        "query": query,
        "type": "auto",
        "numResults": n,
        "contents": {"text": {"maxCharacters": max_chars}},
    }
    if category:
        body["category"] = category
    try:
        r = await client.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json=body,
        )
        r.raise_for_status()
        data: Any = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        return [Source("exa", "search", label, ok=False, error=str(exc), elapsed_s=time.monotonic() - t0, cost_usd=cost)]
    elapsed = time.monotonic() - t0
    results = data.get("results") if isinstance(data, dict) else None
    out: list[Source] = []
    for i, item in enumerate((results or [])[:n] if isinstance(results, list) else []):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        title = str(item.get("title") or "")
        text = str(item.get("text") or "")[:max_chars]
        out.append(
            Source(
                "exa",
                "search",
                label,
                url=url,
                title=title,
                text=text,
                urls=[url] if url else [],
                elapsed_s=elapsed,
                cost_usd=cost if i == 0 else 0.0,
            )
        )
    if not out:
        out.append(Source("exa", "search", label, elapsed_s=elapsed, cost_usd=cost))
    return out


async def tavily_search(client: httpx.AsyncClient, api_key: str, query: str) -> list[Source]:
    t0 = time.monotonic()
    label = f'tavily_search "{query}"'
    cost = 0.005
    try:
        r = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": 10,
                "include_answer": True,
            },
        )
        data: Any = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        return [Source("tavily", "search", label, ok=False, error=str(exc), elapsed_s=time.monotonic() - t0, cost_usd=cost)]
    elapsed = time.monotonic() - t0
    out: list[Source] = []
    answer = str(data.get("answer") or "") if isinstance(data, dict) else ""
    if answer:
        out.append(Source("tavily", "search", label, title="answer", text=answer[:400], cost_usd=cost, elapsed_s=elapsed))
    results = data.get("results") if isinstance(data, dict) else None
    for i, item in enumerate((results or [])[:10] if isinstance(results, list) else []):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        title = str(item.get("title") or "")
        text = str(item.get("content") or "")[:300]
        out.append(
            Source(
                "tavily",
                "search",
                label,
                url=url,
                title=title,
                text=text,
                urls=[url] if url else [],
                elapsed_s=elapsed,
                cost_usd=cost if (i == 0 and not answer) else 0.0,
            )
        )
    if not out:
        out.append(Source("tavily", "search", label, elapsed_s=elapsed, cost_usd=cost))
    return out


def _apify_items_text(items: Any) -> str:
    if not isinstance(items, list):
        return str(items)[:3000]
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("name", "headline", "location", "followerCount", "connectionCount", "summary"):
            val = item.get(key)
            if val:
                lines.append(f"{key}: {val}")
        exp = item.get("experience")
        if isinstance(exp, list):
            for e in exp[:12]:
                if isinstance(e, dict):
                    parts = [str(e.get(k)) for k in ("title", "company", "startDate", "endDate") if e.get(k)]
                    desc = str(e.get("description") or "")[:200]
                    if parts or desc:
                        lines.append("experience: " + " | ".join([*parts, desc])[:300])
        edu = item.get("education")
        if isinstance(edu, list):
            for e in edu[:6]:
                if isinstance(e, dict):
                    parts = [str(e.get(k)) for k in ("school", "degree", "fieldOfStudy", "startDate") if e.get(k)]
                    if parts:
                        lines.append("education: " + " | ".join(parts)[:200])
        posts = item.get("posts")
        if isinstance(posts, list):
            for p in posts[:10]:
                if isinstance(p, dict):
                    text = str(p.get("text") or p.get("content") or "")[:250]
                    if text:
                        lines.append(f"post: {text}")
    return "\n".join(lines)[:4000]


async def apify_linkedin(
    client: httpx.AsyncClient,
    token: str,
    profile_url: str,
    *,
    poll_interval_s: float = 2.0,
    deadline_s: float = 40.0,
) -> Source:
    t0 = time.monotonic()
    label = f"apify_linkedin {profile_url}"
    cost = 0.01
    try:
        r = await client.post(
            f"{APIFY_BASE}/acts/{LINKEDIN_ACTOR}/runs",
            params={"token": token},
            json={"urls": [{"url": profile_url}]},
        )
        run_id = str(r.json().get("data", {}).get("id") or "")
    except (httpx.HTTPError, ValueError) as exc:
        return Source("apify", "linkedin_scrape", label, url=profile_url, ok=False, error=str(exc), elapsed_s=time.monotonic() - t0, cost_usd=cost)
    if not run_id:
        return Source("apify", "linkedin_scrape", label, url=profile_url, ok=False, error="apify run not started", elapsed_s=time.monotonic() - t0, cost_usd=cost)

    deadline = t0 + deadline_s
    while time.monotonic() < deadline:
        try:
            sr = await client.get(f"{APIFY_BASE}/actor-runs/{run_id}", params={"token": token})
            status = str(sr.json().get("data", {}).get("status") or "")
        except (httpx.HTTPError, ValueError) as exc:
            return Source("apify", "linkedin_scrape", label, url=profile_url, ok=False, error=f"poll: {exc}", elapsed_s=time.monotonic() - t0, cost_usd=cost)
        if status == "SUCCEEDED":
            try:
                rr = await client.get(f"{APIFY_BASE}/actor-runs/{run_id}/dataset/items", params={"token": token})
                items: Any = rr.json()
            except (httpx.HTTPError, ValueError) as exc:
                return Source("apify", "linkedin_scrape", label, url=profile_url, ok=False, error=f"results: {exc}", elapsed_s=time.monotonic() - t0, cost_usd=cost)
            return Source(
                "apify",
                "linkedin_scrape",
                label,
                url=profile_url,
                title=str((items[0].get("name") if isinstance(items, list) and isinstance(items[0], dict) else "") or profile_url),
                text=_apify_items_text(items),
                elapsed_s=time.monotonic() - t0,
                cost_usd=cost,
            )
        if status in {"FAILED", "ABORTED", "TIMED-OUT"}:
            return Source("apify", "linkedin_scrape", label, url=profile_url, ok=False, error=f"apify run {status}", elapsed_s=time.monotonic() - t0, cost_usd=cost)
        await asyncio.sleep(poll_interval_s)

    return Source("apify", "linkedin_scrape", label, url=profile_url, ok=False, error="apify run pending at deadline", elapsed_s=time.monotonic() - t0, cost_usd=cost)
