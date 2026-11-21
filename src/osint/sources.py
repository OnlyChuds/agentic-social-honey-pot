"""Normalized OSINT source records and heuristic URL selection (no LLM involved)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://[^\s\"'<>)\]}`|]+")
_LINKEDIN_IN_RE = re.compile(r"https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9_\-]+", re.IGNORECASE)
_SOCIAL_HOSTS = (
    "linkedin.com",
    "instagram.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "github.com",
    "youtube.com",
    "tiktok.com",
)
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mp3", ".svg")
_JUNK_HOSTS = (
    "schema.org",
    "w3.org",
    "gstatic.com",
    "google.com",
    "googletagmanager.com",
    "cloudflare.com",
    "gravatar.com",
)

READ_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("funding", 2),
    ("raised", 2),
    ("seed", 2),
    ("press", 2),
    ("author", 2),
    ("speaker", 2),
    ("podcast", 2),
    ("interview", 2),
    ("keynote", 2),
    ("ciso", 2),
    ("about", 1),
    ("blog", 1),
    ("team", 1),
    ("talk", 1),
    ("panel", 1),
    ("event", 1),
    ("company", 1),
    ("security", 1),
    ("cyber", 1),
)


@dataclass(slots=True)
class Source:
    """One unit of OSINT evidence: a search hit, a page read, or a profile scrape."""

    provider: str
    kind: str  # "search" | "read" | "linkedin_scrape" | "error"
    label: str
    url: str = ""
    title: str = ""
    text: str = ""
    urls: list[str] = field(default_factory=list)
    ok: bool = True
    error: str = ""
    elapsed_s: float = 0.0
    cost_usd: float = 0.0


def extract_urls(text: str, limit: int = 25) -> list[str]:
    out: list[str] = []
    for raw in _URL_RE.findall(text):
        url = raw.rstrip(".,;:)]}\"'")
        if url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


def host_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _name_tokens(name: str) -> set[str]:
    return {tok for tok in re.findall(r"[a-z]+", name.lower()) if len(tok) > 2}


def find_linkedin_url(sources: list[Source], name: str) -> str | None:
    """Pick the LinkedIn profile URL that best matches the target's name."""
    tokens = _name_tokens(name)
    fallback: str | None = None
    for src in sources:
        for url in src.urls or ([src.url] if src.url else []):
            if not _LINKEDIN_IN_RE.match(url):
                continue
            if fallback is None:
                fallback = url
            slug = url.rstrip("/").rsplit("/", 1)[-1].lower()
            title = src.title.lower()
            if any(tok in slug for tok in tokens):
                return url
            if any(len(tok) > 4 and tok in title for tok in tokens):
                return url
    return fallback


def pick_read_urls(
    sources: list[Source],
    *,
    org: str,
    name: str,
    exclude: set[str] | None = None,
    limit: int = 3,
) -> list[str]:
    """Score candidate URLs for page reads: org domain + content keywords first, social last."""
    org_l = org.lower()
    tokens = _name_tokens(name)
    excluded = set(exclude or ())
    seen: set[str] = set()
    scored: list[tuple[int, str]] = []
    for src in sources:
        for url in src.urls or ([src.url] if src.url else []):
            norm = url.rstrip("/").lower()
            if norm in seen or url in excluded:
                continue
            seen.add(norm)
            if _LINKEDIN_IN_RE.match(url):
                continue
            host = host_of(url)
            if not host or "." not in host:
                continue
            if any(host == junk or host.endswith("." + junk) for junk in _JUNK_HOSTS):
                continue
            if any(host == soc or host.endswith("." + soc) for soc in _SOCIAL_HOSTS):
                continue
            if url.lower().endswith(_IMAGE_EXT):
                continue
            score = 0
            if org_l and org_l in host:
                score += 5
            blob = f"{src.title} {src.text[:300]}".lower()
            for kw, pts in READ_KEYWORDS:
                if kw in blob:
                    score += pts
            title_l = src.title.lower()
            if any(tok in title_l for tok in tokens):
                score += 2
            scored.append((score, url))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [url for _score, url in scored[:limit]]
