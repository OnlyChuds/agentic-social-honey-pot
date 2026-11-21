"""Deterministic generator for plausible lookalike (deceptive) domains.

The challenge rules require sender addresses and landing-page domains to be
*plausible lookalikes* of the target organization's real root domain, while
never using the real root domain or any authentic third-party domain (which
would void the session). ``src/agents/qa.py`` provides the defensive
``check_domain_blocklist`` gate; this module is the offensive half: it
generates candidate lookalikes that pass that gate by construction.

Techniques (in decreasing order of real-world plausibility):
  1. Hyphenated service variant on the real TLD  -- ``revel8-portal.com``
  2. Subdomain of a TLD-swapped root             -- ``sso.revel8.net``
  3. Hyphenated service variant on a swapped TLD -- ``revel8-secure.de``
  4. Plain TLD swap of the root                  -- ``revel8.net``
  5. Homoglyph substitution of the brand         -- ``reve18.com``
  6. Homoglyph brand + hyphenated variant        -- ``reve18-portal.com``

Every candidate is validated for hostname syntax and screened against the
blocked root-domain set (both the input domain and its registrable apex root
are always treated as blocked, so a candidate can never be the real domain
or a subdomain of it).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from src.agents.qa import (
    DEFAULT_BLOCKED_ROOT_DOMAINS,
    extract_domain,
    is_domain_blocked,
    is_valid_hostname,
)

# Suffixes that read as internal/portal services when hyphenated onto a brand.
SERVICE_SUFFIXES: tuple[str, ...] = (
    "portal",
    "login",
    "secure",
    "auth",
    "accounts",
    "signin",
    "verify",
    "sso",
    "access",
    "id",
    "gateway",
    "hub",
    "cloud",
    "web",
    "team",
    "ops",
    "it",
    "hq",
    "sys",
    "security",
)

# Subdomain labels commonly used for identity/portal entry points.
SUBDOMAIN_PREFIXES: tuple[str, ...] = (
    "portal",
    "sso",
    "login",
    "auth",
    "accounts",
    "id",
    "signin",
    "web",
    "it",
    "secure",
    "help",
    "support",
)

# TLDs plausible for a European tech company, tried in order.
ALTERNATE_TLDS: tuple[str, ...] = (
    "net",
    "de",
    "io",
    "co",
    "org",
    "dev",
    "app",
    "eu",
    "us",
    "cloud",
    "systems",
    "security",
    "digital",
    "tech",
    "online",
    "services",
)

# Common two-label public suffixes so e.g. ``www.acme.co.uk`` splits as ``("acme", "co.uk")``.
_MULTI_PART_TLDS: frozenset[str] = frozenset(
    {
        "ac.uk",
        "co.de",
        "co.jp",
        "co.nz",
        "co.uk",
        "co.za",
        "com.au",
        "com.br",
        "com.de",
        "com.sg",
        "org.uk",
    }
)

# Character confusions that keep the brand readable (l/1, o/0, i/l, rn/m).
_CONFUSION_MAP: tuple[tuple[str, str], ...] = (
    ("l", "1"),
    ("1", "l"),
    ("o", "0"),
    ("0", "o"),
    ("i", "l"),
    ("rn", "m"),
    ("m", "rn"),
)

# Pretext/concept keyword hints mapped to the most plausible service suffixes first.
_HINT_SUFFIX_PRIORITY: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("mfa", "2fa", "otp", "authenticator", "device", "verify"), ("verify", "auth", "id", "sso")),
    (("password", "reset", "rotation", "sso", "login", "signin"), ("sso", "login", "auth", "id")),
    (("invoice", "billing", "payment", "finance", "vendor", "accounting"), ("billing", "accounts", "portal")),
    (("document", "vault", "contract", "signed", "nda", "file", "share"), ("docs", "vault", "portal", "secure")),
    (("schedule", "event", "panel", "conference", "speaker", "workshop"), ("events", "portal", "hub", "team")),
    (("it", "helpdesk", "ops", "infra", "cloud", "support"), ("ops", "it", "support", "portal")),
    (("security", "compliance", "audit", "soc"), ("security", "verify", "portal", "auth")),
)

_GERMAN_ASCII_MAP: dict[int, str] = str.maketrans(
    {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
    }
)
_HONORIFICS: frozenset[str] = frozenset(
    {"dr", "prof", "mr", "mrs", "ms", "mx", "herr", "frau", "ing", "dipl"}
)
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _split_host(real_root: str) -> tuple[str, str, str]:
    """Extract ``(brand_base, tld, apex_root)`` from a domain, email, or URL.

    Examples:
      - ``"revel8.com"`` -> ``("revel8", "com", "revel8.com")``
      - ``"https://www.revel8.com/login"`` -> ``("revel8", "com", "revel8.com")``
      - ``"portal.acme.co.uk"`` -> ``("acme", "co.uk", "acme.co.uk")``
    """
    host = extract_domain(real_root)
    if not host or "." not in host:
        return host, "", host

    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return host, "", host

    if len(parts) >= 3 and f"{parts[-2]}.{parts[-1]}" in _MULTI_PART_TLDS:
        tld = f"{parts[-2]}.{parts[-1]}"
        base = parts[-3]
    else:
        tld = parts[-1]
        base = parts[-2]

    return base, tld, f"{base}.{tld}"


def _confused_variants(base: str) -> list[str]:
    """One-confusion-at-a-time homoglyph variants of the brand base."""
    variants: list[str] = []
    seen: set[str] = set()
    for pattern, replacement in _CONFUSION_MAP:
        start = 0
        while True:
            idx = base.find(pattern, start)
            if idx == -1:
                break
            variant = base[:idx] + replacement + base[idx + len(pattern) :]
            if variant != base and variant not in seen:
                seen.add(variant)
                variants.append(variant)
            start = idx + 1
    return variants


def _ordered_suffixes(hint: str | None) -> tuple[str, ...]:
    """Return service suffixes ordered for ``hint`` (or default ``SERVICE_SUFFIXES``)."""
    if not hint or not hint.strip():
        return SERVICE_SUFFIXES

    lower_hint = hint.strip().lower()
    prioritized: list[str] = []
    for keywords, suffixes in _HINT_SUFFIX_PRIORITY:
        if any(re.search(rf"\b{re.escape(kw)}\b", lower_hint) for kw in keywords):
            for sfx in suffixes:
                if sfx not in prioritized:
                    prioritized.append(sfx)

    if not prioritized:
        return SERVICE_SUFFIXES

    for sfx in SERVICE_SUFFIXES:
        if sfx not in prioritized:
            prioritized.append(sfx)
    return tuple(prioritized)


def _build_blocked_set(
    real_root: str,
    apex_root: str,
    blocked_root_domains: Iterable[str],
    extra_blocked: Iterable[str],
) -> set[str]:
    blocked: set[str] = set()
    for candidate_root in (*blocked_root_domains, *extra_blocked, real_root, apex_root):
        root = extract_domain(candidate_root)
        if root:
            blocked.add(root)
    return blocked


def generate_lookalike_domains(
    real_root: str,
    *,
    blocked_root_domains: Iterable[str] = DEFAULT_BLOCKED_ROOT_DOMAINS,
    extra_blocked: Iterable[str] = (),
    limit: int = 20,
    hint: str | None = None,
) -> list[str]:
    """Generate syntactically valid, non-blocked lookalike domains for ``real_root``.

    Both ``real_root`` and its registrable apex root are always treated as
    blocked, so no candidate is the real domain or a subdomain of it.
    Candidates are returned in plausibility order, deduplicated, capped at
    ``limit``. Pass an optional ``hint`` (e.g. sender concept or landing-page
    pretext) to prefer matching service suffixes first.
    """
    if limit <= 0:
        return []

    base, real_tld, apex_root = _split_host(real_root)
    if not base or not real_tld:
        return []

    blocked = _build_blocked_set(real_root, apex_root, blocked_root_domains, extra_blocked)
    suffixes = _ordered_suffixes(hint)

    candidates: list[str] = []
    seen: set[str] = set()

    def _emit(candidate: str) -> None:
        if candidate in seen or len(candidates) >= limit:
            return
        seen.add(candidate)
        if not is_valid_hostname(candidate):
            return
        if is_domain_blocked(candidate, blocked) is not None:
            return
        candidates.append(candidate)

    # 1. Hyphenated service variant on the real TLD.
    for suffix in suffixes:
        _emit(f"{base}-{suffix}.{real_tld}")

    # 2. Subdomain of a TLD-swapped root.
    for tld in ALTERNATE_TLDS:
        if tld == real_tld:
            continue
        for prefix in SUBDOMAIN_PREFIXES[:6]:
            _emit(f"{prefix}.{base}.{tld}")

    # 3. Hyphenated service variant on a swapped TLD.
    for tld in ALTERNATE_TLDS[:8]:
        if tld == real_tld:
            continue
        for suffix in suffixes[:10]:
            _emit(f"{base}-{suffix}.{tld}")

    # 4. Plain TLD swap of the root.
    for tld in ALTERNATE_TLDS:
        if tld != real_tld:
            _emit(f"{base}.{tld}")

    # 5-6. Homoglyph brand, with and without a service suffix.
    for confused in _confused_variants(base):
        _emit(f"{confused}.{real_tld}")
        for suffix in suffixes[:6]:
            _emit(f"{confused}-{suffix}.{real_tld}")

    return candidates


def pick_lookalike_domain(
    real_root: str,
    *,
    blocked_root_domains: Iterable[str] = DEFAULT_BLOCKED_ROOT_DOMAINS,
    extra_blocked: Iterable[str] = (),
    hint: str | None = None,
) -> str:
    """Return the most plausible non-blocked lookalike for ``real_root`` ('' if none)."""
    candidates = generate_lookalike_domains(
        real_root,
        blocked_root_domains=blocked_root_domains,
        extra_blocked=extra_blocked,
        limit=1,
        hint=hint,
    )
    return candidates[0] if candidates else ""


def ensure_lookalike_domain(
    candidate: str,
    real_root: str,
    *,
    blocked_root_domains: Iterable[str] = DEFAULT_BLOCKED_ROOT_DOMAINS,
    extra_blocked: Iterable[str] = (),
    hint: str | None = None,
) -> str:
    """Return ``candidate`` when valid and non-blocked; otherwise pick a safe lookalike."""
    _, _, apex_root = _split_host(real_root)
    blocked = _build_blocked_set(real_root, apex_root, blocked_root_domains, extra_blocked)
    host = extract_domain(candidate)
    if host and is_valid_hostname(host) and is_domain_blocked(host, blocked) is None:
        return host
    return pick_lookalike_domain(
        real_root,
        blocked_root_domains=blocked_root_domains,
        extra_blocked=extra_blocked,
        hint=hint,
    )


def _ascii_name_tokens(sender_name: str) -> list[str]:
    lowered = sender_name.strip().lower().translate(_GERMAN_ASCII_MAP)
    folded = unicodedata.normalize("NFKD", lowered).encode("ascii", "ignore").decode("ascii")
    tokens = _ASCII_TOKEN_RE.findall(folded)
    while len(tokens) > 1 and tokens[0] in _HONORIFICS:
        tokens.pop(0)
    return tokens


def derive_sender_email(sender_name: str, domain: str) -> str:
    """Build a plausible ASCII lowercase ``first.last@domain`` address from a display name."""
    clean_domain = extract_domain(domain)
    if not clean_domain:
        return ""
    tokens = _ascii_name_tokens(sender_name)
    if not tokens:
        local = "info"
    elif len(tokens) == 1:
        local = tokens[0]
    else:
        local = f"{tokens[0]}.{tokens[-1]}"
    return f"{local}@{clean_domain}"
