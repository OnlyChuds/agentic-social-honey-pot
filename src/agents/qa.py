from __future__ import annotations

import base64
import html as html_lib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from src.agents.base import BaseAgent
from src.llm.text import TextLLMClient
from src.models import BrandingManifest, LandingPageSpec, Proposal, QAIssue, QAVerdict, Snapshot
from src.render.screenshot import (
    HtmlRenderer,
    RenderedField,
    RenderedLink,
    RenderMode,
    RenderResult,
)

logger = logging.getLogger(__name__)

DEFAULT_BLOCKED_ROOT_DOMAINS: frozenset[str] = frozenset(
    {
        "allianz.com",
        "amazon.com",
        "apple.com",
        "cloudflare.com",
        "dhl.com",
        "docusign.com",
        "github.com",
        "google.com",
        "linkedin.com",
        "meta.com",
        "microsoft.com",
        "okta.com",
        "openai.com",
        "paypal.com",
        "revel8.com",
        "revel8.io",
        "slack.com",
        "stackit.cloud",
        "stripe.com",
        "zoom.us",
    }
)

_HOSTNAME_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TLD_RE = re.compile(r"^[a-z]{2,24}$")
_STYLE_SCRIPT_RE = re.compile(r"<(?:style|script)\b[^>]*>[\s\S]*?</(?:style|script)>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_TEXT_URL_DOMAIN_RE = re.compile(
    r"(?:https?://)?([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+)",
    re.IGNORECASE,
)

_PLACEHOLDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("TODO/FIXME/TBD marker", re.compile(r"\b(?:TODO|FIXME|TBD|XXX)\b")),
    ("Lorem ipsum placeholder", re.compile(r"\blorem\s+ipsum\b", re.IGNORECASE)),
    (
        "Bracketed placeholder",
        re.compile(
            r"\[(?:name|first[\s_-]?name|last[\s_-]?name|company|org|link|url|date|title|sender|insert[^\]]*)\]",
            re.IGNORECASE,
        ),
    ),
    ("Double-brace template token", re.compile(r"\{\{[^{}]+\}\}")),
    ("Single-brace variable token", re.compile(r"\{[A-Za-z_][A-Za-z0-9_.-]{0,30}\}")),
    ("Example placeholder domain", re.compile(r"\bexample\.(?:com|org|net)\b", re.IGNORECASE)),
)

_DISALLOWED_META_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bignore\s+(?:all\s+)?(?:previous|prior)\s+instructions\b", re.IGNORECASE),
    re.compile(r"\bdisregard\s+(?:the\s+)?system\s+prompt\b", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\s+in\s+developer\s+mode\b", re.IGNORECASE),
)

_RULE_COMPLIANCE_CRITERIA: frozenset[str] = frozenset({"rule_compliance", "domain_blocklist"})


class _LLMQAIssue(BaseModel):
    criterion: str
    severity: Literal["critical", "major", "minor"]
    description: str = ""
    fix: str


class _LLMQAResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    passed: bool = Field(alias="pass")
    scores: dict[str, int] = Field(default_factory=dict)
    issues: list[_LLMQAIssue] = Field(default_factory=list)
    required_fixes: list[str] = Field(default_factory=list)
    optional_fixes: list[str] = Field(default_factory=list)


def extract_domain(value: str) -> str:
    """Extract a normalized lowercase hostname from a domain, email address, or URL."""
    raw = value.strip().lower()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urlparse(raw)
        host = parsed.hostname or ""
        return host.rstrip(".")
    if "@" in raw and " " not in raw:
        _, _, domain_part = raw.rpartition("@")
        return domain_part.split("/", 1)[0].split(":", 1)[0].rstrip(".")
    candidate = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].split(":", 1)[0]
    return candidate.rstrip(".")


def is_valid_hostname(domain: str) -> bool:
    """Return True when ``domain`` is a syntactically valid multi-label hostname."""
    host = extract_domain(domain)
    if not host or len(host) > 253 or "." not in host:
        return False
    labels = host.split(".")
    if not _TLD_RE.match(labels[-1]):
        return False
    return all(bool(_HOSTNAME_LABEL_RE.match(label)) for label in labels)


def is_domain_blocked(domain: str, blocked_root_domains: Iterable[str]) -> str | None:
    """Return the matched blocked root domain if ``domain`` equals or is a subdomain of one."""
    host = extract_domain(domain)
    if not host:
        return None
    for blocked in blocked_root_domains:
        root = extract_domain(blocked)
        if not root:
            continue
        if host == root or host.endswith(f".{root}"):
            return root
    return None


def check_domain_blocklist(
    domains: Iterable[str],
    *,
    blocked_root_domains: Iterable[str] = DEFAULT_BLOCKED_ROOT_DOMAINS,
    extra_blocked: Iterable[str] = (),
) -> list[QAIssue]:
    """Validate hostname syntax and ensure no real/blocked root domain is used."""
    all_blocked = {extract_domain(d) for d in (*blocked_root_domains, *extra_blocked) if d.strip()}
    all_blocked.discard("")
    issues: list[QAIssue] = []
    seen: set[str] = set()

    for raw in domains:
        host = extract_domain(raw)
        if not host or host in seen:
            continue
        seen.add(host)
        if not is_valid_hostname(host):
            issues.append(
                QAIssue(
                    criterion="domain_blocklist",
                    severity="critical",
                    fix=f"Replace invalid domain '{raw}' with a syntactically valid domain.",
                )
            )
            continue
        matched = is_domain_blocked(host, all_blocked)
        if matched is not None:
            issues.append(
                QAIssue(
                    criterion="domain_blocklist",
                    severity="critical",
                    fix=(
                        f"Domain '{host}' uses blocked real root domain '{matched}'. "
                        "Replace it with a non-blocked simulation domain."
                    ),
                )
            )
    return issues


def _strip_html_to_text(html_source: str) -> str:
    without_blocks = _STYLE_SCRIPT_RE.sub(" ", html_source)
    without_tags = _HTML_TAG_RE.sub(" ", without_blocks)
    return html_lib.unescape(without_tags)


def check_placeholders(visible_text: str, html_source: str = "") -> list[QAIssue]:
    """Scan visible text and stripped HTML content for unresolved placeholders or dev artifacts."""
    combined = f"{visible_text}\n{_strip_html_to_text(html_source)}" if html_source else visible_text
    issues: list[QAIssue] = []
    seen: set[tuple[str, str]] = set()

    for label, pattern in _PLACEHOLDER_PATTERNS:
        for match in pattern.finditer(combined):
            token = match.group(0)
            key = (label, token.lower())
            if key in seen:
                continue
            seen.add(key)
            issues.append(
                QAIssue(
                    criterion="cleanliness",
                    severity="critical",
                    fix=f"Remove or replace {label} '{token}' before sending.",
                )
            )
    return issues


def check_links(
    links: Sequence[RenderedLink],
    *,
    blocked_root_domains: Iterable[str] = DEFAULT_BLOCKED_ROOT_DOMAINS,
    extra_blocked: Iterable[str] = (),
    expected_url: str | None = None,
) -> list[QAIssue]:
    """Check rendered anchor tags for dead links, blocked domains, and text-vs-href mismatches."""
    all_blocked = {extract_domain(d) for d in (*blocked_root_domains, *extra_blocked) if d.strip()}
    all_blocked.discard("")
    issues: list[QAIssue] = []
    matched_expected = expected_url is None

    expected_parsed = urlparse(expected_url) if expected_url else None

    for link in links:
        href = link.href.strip()
        text = link.text.strip()
        lower_href = href.lower()

        if not href or href == "#" or lower_href.startswith("javascript:"):
            issues.append(
                QAIssue(
                    criterion="detail_precision",
                    severity="critical",
                    fix=f"Replace dead or empty link href='{href}' (text='{text}') with a valid URL.",
                )
            )
            continue

        if lower_href.startswith(("mailto:", "tel:")):
            addr = href.split(":", 1)[1]
            if lower_href.startswith("mailto:"):
                blocked = is_domain_blocked(addr, all_blocked)
                if blocked is not None:
                    issues.append(
                        QAIssue(
                            criterion="domain_blocklist",
                            severity="critical",
                            fix=f"mailto link '{href}' uses blocked root domain '{blocked}'.",
                        )
                    )
            continue

        parsed_href = urlparse(href)
        href_host = (parsed_href.hostname or "").lower().rstrip(".")

        if href_host in {"localhost", "127.0.0.1", "0.0.0.0"}:
            issues.append(
                QAIssue(
                    criterion="detail_precision",
                    severity="critical",
                    fix=f"Replace localhost link '{href}' with the registered simulation URL.",
                )
            )
            continue

        blocked = is_domain_blocked(href_host, all_blocked)
        if blocked is not None:
            issues.append(
                QAIssue(
                    criterion="domain_blocklist",
                    severity="critical",
                    fix=f"Link '{href}' points to blocked root domain '{blocked}'.",
                )
            )

        # If visible text displays a domain/URL, ensure it aligns with href host
        if text and href_host:
            for m in _TEXT_URL_DOMAIN_RE.finditer(text):
                shown_domain = m.group(1).lower().rstrip(".")
                if not is_valid_hostname(shown_domain):
                    continue
                if shown_domain != href_host and not href_host.endswith(f".{shown_domain}"):
                    issues.append(
                        QAIssue(
                            criterion="detail_precision",
                            severity="critical",
                            fix=(
                                f"Anchor text displays domain '{shown_domain}' but href points to "
                                f"'{href_host}'. Align link text with destination URL."
                            ),
                        )
                    )
                    break

        if expected_parsed is not None and not matched_expected:
            exp_host = (expected_parsed.hostname or "").lower().rstrip(".")
            exp_path = expected_parsed.path.rstrip("/")
            if href_host == exp_host and parsed_href.path.rstrip("/") == exp_path:
                matched_expected = True

    if expected_url is not None and not matched_expected:
        issues.append(
            QAIssue(
                criterion="detail_precision",
                severity="major",
                fix=f"Expected link to '{expected_url}' was not found in rendered HTML.",
            )
        )

    return issues


def check_render_health(render_result: RenderResult) -> list[QAIssue]:
    """Convert Playwright render telemetry into deterministic cleanliness QA issues."""
    issues: list[QAIssue] = []

    for uri in render_result.unresolved_assets:
        issues.append(
            QAIssue(
                criterion="cleanliness",
                severity="critical",
                fix=f"Resolve missing asset reference '{uri}' or remove the broken <img> tag.",
            )
        )

    for src in render_result.broken_images:
        if src in render_result.unresolved_assets:
            continue
        issues.append(
            QAIssue(
                criterion="cleanliness",
                severity="critical",
                fix=f"Fix broken image '{src}' so it loads with non-zero dimensions.",
            )
        )

    for url in render_result.external_requests:
        issues.append(
            QAIssue(
                criterion="cleanliness",
                severity="critical",
                fix=(
                    f"Remove external network request '{url}'; rendered HTML must be self-contained "
                    "(inline CSS/SVG or asset:// images)."
                ),
            )
        )

    if render_result.has_horizontal_overflow:
        issues.append(
            QAIssue(
                criterion="cleanliness",
                severity="major",
                fix="Fix horizontal layout overflow so content fits cleanly within the viewport.",
            )
        )

    return issues


def check_manifest_consistency(
    visible_text: str,
    manifest: BrandingManifest | None,
    *,
    blocked_root_domains: Iterable[str] = DEFAULT_BLOCKED_ROOT_DOMAINS,
    extra_blocked: Iterable[str] = (),
) -> list[QAIssue]:
    """Verify rendered artifact aligns with the active BrandingManifest."""
    if manifest is None:
        return []

    issues: list[QAIssue] = []
    manifest_domains = [d for d in (manifest.domain, manifest.email) if d.strip()]
    issues.extend(
        check_domain_blocklist(
            manifest_domains,
            blocked_root_domains=blocked_root_domains,
            extra_blocked=extra_blocked,
        )
    )

    lower_text = visible_text.lower()
    if (
        manifest.company
        and manifest.company.lower() not in lower_text
        and (not manifest.sender_name or manifest.sender_name.lower() not in lower_text)
    ):
        issues.append(
            QAIssue(
                criterion="sender_authenticity",
                severity="major",
                fix=(
                    f"Include expected sender identity ('{manifest.sender_name}' or "
                    f"'{manifest.company}') from the branding manifest."
                ),
            )
        )
    return issues


MAX_PAGE_FIELDS = 20
_SCRIPT_TAG_RE = re.compile(r"<script\b", re.IGNORECASE)


def check_form_fields(
    rendered_fields: Sequence[RenderedField],
    required_fields: Sequence[str],
) -> list[QAIssue]:
    """Verify rendered HTML contains form inputs for every declared field name (<= 20 limit)."""
    issues: list[QAIssue] = []
    if len(required_fields) > MAX_PAGE_FIELDS:
        issues.append(
            QAIssue(
                criterion="format_appropriateness",
                severity="critical",
                fix=(
                    f"Page declares {len(required_fields)} fields, exceeding Target API limit "
                    f"of {MAX_PAGE_FIELDS}."
                ),
            )
        )
    if not required_fields:
        return issues

    present_tokens: set[str] = set()
    for field in rendered_fields:
        for val in (field.name, field.element_id, field.field_type, field.placeholder):
            cleaned = val.strip().lower()
            if cleaned:
                present_tokens.add(cleaned)

    for req in required_fields:
        needle = req.strip().lower()
        if not needle:
            continue
        if not any(needle == tok or needle in tok for tok in present_tokens):
            issues.append(
                QAIssue(
                    criterion="format_appropriateness",
                    severity="critical",
                    fix=f"Add a form input field matching declared field '{req}'.",
                )
            )
    return issues


def check_disallowed_meta_text(text: str) -> list[QAIssue]:
    """Guard against simulation-voiding meta-instruction phrasing."""
    issues: list[QAIssue] = []
    for pattern in _DISALLOWED_META_PATTERNS:
        match = pattern.search(text)
        if match:
            issues.append(
                QAIssue(
                    criterion="rule_compliance",
                    severity="critical",
                    fix=f"Remove disallowed meta-instruction phrase '{match.group(0)}'.",
                )
            )
    return issues


def run_deterministic_checks(
    *,
    html: str,
    render_result: RenderResult | None = None,
    manifest: BrandingManifest | None = None,
    page_url: str | None = None,
    expected_url: str | None = None,
    landing_page_spec: LandingPageSpec | None = None,
    extra_blocked_domains: Iterable[str] = (),
) -> list[QAIssue]:
    """Execute all code-level static and rendered QA checks."""
    issues: list[QAIssue] = []
    visible_text = (
        render_result.visible_text if render_result is not None else _strip_html_to_text(html)
    )

    if render_result is not None:
        issues.extend(check_render_health(render_result))
        issues.extend(
            check_links(
                render_result.links,
                extra_blocked=extra_blocked_domains,
                expected_url=expected_url,
            )
        )
        if landing_page_spec is not None:
            issues.extend(check_form_fields(render_result.form_fields, landing_page_spec.fields))

    if page_url:
        issues.extend(check_domain_blocklist([page_url], extra_blocked=extra_blocked_domains))

    if _SCRIPT_TAG_RE.search(html):
        issues.append(
            QAIssue(
                criterion="cleanliness",
                severity="critical",
                fix="Remove <script> tags; Target API renders HTML with scripts disabled.",
            )
        )

    issues.extend(check_placeholders(visible_text, html))
    issues.extend(
        check_manifest_consistency(
            visible_text,
            manifest,
            extra_blocked=extra_blocked_domains,
        )
    )
    issues.extend(check_disallowed_meta_text(visible_text))
    return issues


@dataclass(slots=True)
class QALoopResult:
    artifact: Any
    verdict: QAVerdict
    proposal: Proposal
    cycles_used: int
    render_result: RenderResult | None = None


class QAAgent(BaseAgent):
    """Pre-send visual and deterministic craft QA gate returning a QAVerdict proposal."""

    agent_name = "qa"
    proposal_kind = "qa_verdict"

    def __init__(
        self,
        llm: TextLLMClient,
        *,
        renderer: HtmlRenderer | None = None,
        max_revisions: int = 2,
        vision_enabled: bool = False,
        prompts_dir: Path = Path("prompts"),
    ) -> None:
        super().__init__(llm, prompts_dir=prompts_dir)
        self.renderer = renderer
        self.max_revisions = max_revisions
        self.vision_enabled = vision_enabled

    async def _call_llm_review(
        self,
        context_payload: dict[str, Any],
        png_bytes: bytes | None,
    ) -> _LLMQAResponse:
        text_messages = self.build_messages(context_payload)
        if self.vision_enabled and png_bytes:
            b64 = base64.b64encode(png_bytes).decode("ascii")
            vision_messages: list[dict[str, Any]] = [
                {"role": "system", "content": self.prompt.system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": json.dumps(context_payload, indent=2)},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                },
            ]
            try:
                return await self.llm.complete_json(
                    vision_messages,
                    _LLMQAResponse,
                    temperature=self.prompt.temperature,
                    max_tokens=self.prompt.max_tokens,
                    retries=1,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Vision QA call failed (%s); falling back to text + DOM telemetry.",
                    type(exc).__name__,
                )

        return await self.llm.complete_json(
            text_messages,
            _LLMQAResponse,
            temperature=self.prompt.temperature,
            max_tokens=self.prompt.max_tokens,
            retries=2,
        )

    async def evaluate(
        self,
        snapshot: Snapshot,
        *,
        html: str,
        mode: RenderMode = "email",
        channel: str = "email",
        asset_map: dict[str, Path] | None = None,
        save_screenshot_path: Path | None = None,
        page_url: str | None = None,
        expected_url: str | None = None,
        landing_page_spec: LandingPageSpec | None = None,
        extra_blocked_domains: Iterable[str] = (),
        revision_cycle: int = 0,
        render: bool = True,
        run_llm: bool = True,
    ) -> tuple[QAVerdict, Proposal, RenderResult | None]:
        """Render HTML, run deterministic checks + optional LLM review, and emit a Proposal."""
        render_result: RenderResult | None = None
        if render:
            if self.renderer is not None:
                render_result = await self.renderer.render(
                    html,
                    mode=mode,
                    asset_map=asset_map,
                    save_path=save_screenshot_path,
                )
            else:
                async with HtmlRenderer() as temp_renderer:
                    render_result = await temp_renderer.render(
                        html,
                        mode=mode,
                        asset_map=asset_map,
                        save_path=save_screenshot_path,
                    )

        code_issues = run_deterministic_checks(
            html=html,
            render_result=render_result,
            manifest=snapshot.identity,
            page_url=page_url,
            expected_url=expected_url,
            landing_page_spec=landing_page_spec,
            extra_blocked_domains=extra_blocked_domains,
        )

        llm_issues: list[QAIssue] = []
        llm_scores: dict[str, int] = {}
        required_fixes: list[str] = []
        optional_fixes: list[str] = []

        if run_llm:
            context_payload: dict[str, Any] = {
                "channel": channel,
                "render_mode": mode,
                "target": snapshot.target.model_dump(mode="json"),
                "manifest": (
                    snapshot.identity.model_dump(mode="json")
                    if snapshot.identity is not None
                    else None
                ),
                "html": html,
                "render_telemetry": (
                    render_result.to_summary_dict() if render_result is not None else None
                ),
                "deterministic_checks": [issue.model_dump(mode="json") for issue in code_issues],
                "revision_cycle": revision_cycle,
                "max_revisions": self.max_revisions,
            }
            llm_resp = await self._call_llm_review(
                context_payload,
                render_result.png_bytes if render_result is not None else None,
            )
            llm_scores = dict(llm_resp.scores)
            llm_issues = [
                QAIssue(
                    criterion=item.criterion,
                    severity=item.severity,
                    fix=item.fix if not item.description else f"{item.description} — {item.fix}",
                )
                for item in llm_resp.issues
            ]
            required_fixes.extend(llm_resp.required_fixes)
            optional_fixes.extend(llm_resp.optional_fixes)

        all_issues = [*code_issues, *llm_issues]
        for issue in code_issues:
            if issue.severity in {"critical", "major"} and issue.fix not in required_fixes:
                required_fixes.append(issue.fix)
            elif issue.severity == "minor" and issue.fix not in optional_fixes:
                optional_fixes.append(issue.fix)

        has_rule_violation = any(
            i.severity == "critical" and i.criterion in _RULE_COMPLIANCE_CRITERIA
            for i in all_issues
        )
        has_critical = any(i.severity == "critical" for i in all_issues)
        has_major = any(i.severity == "major" for i in all_issues)

        ship_with_warning = False
        if has_rule_violation:
            passed = False
        elif has_critical or (has_major and revision_cycle < self.max_revisions):
            if revision_cycle >= self.max_revisions:
                passed = True
                ship_with_warning = True
            else:
                passed = False
        else:
            passed = True

        verdict = QAVerdict.model_validate(
            {
                "pass": passed,
                "issues": all_issues,
                "required_fixes": required_fixes,
                "optional_fixes": optional_fixes,
            }
        )

        payload = verdict.model_dump(mode="json", by_alias=True)
        payload["scores"] = llm_scores
        payload["revision_cycle"] = revision_cycle
        payload["ship_with_warning"] = ship_with_warning
        if render_result is not None:
            payload["render_summary"] = render_result.to_summary_dict()

        rationale = (
            f"QA {'passed' if passed else 'failed'} on cycle {revision_cycle} "
            f"({len(all_issues)} issues"
            + (", ship_with_warning=True" if ship_with_warning else "")
            + ")."
        )
        proposal = self.make_proposal(snapshot, payload, rationale=rationale)
        return verdict, proposal, render_result

    async def run(self, snapshot: Snapshot, **kwargs: Any) -> Proposal:
        """Shared BaseAgent entrypoint: returns a Proposal(kind='qa_verdict')."""
        html = str(kwargs["html"])
        is_landing_page = bool(kwargs.get("is_landing_page", False))
        mode: RenderMode = kwargs.get("mode") or ("landing_page" if is_landing_page else "email")
        revision_raw = kwargs.get("revision_cycle", kwargs.get("revision", 0))

        asset_map: dict[str, Path] | None = kwargs.get("asset_map")
        if (
            asset_map is None
            and snapshot.identity is not None
            and snapshot.identity.portrait.api_asset_id
            and snapshot.identity.portrait.local
        ):
            local_portrait = Path(snapshot.identity.portrait.local)
            if local_portrait.is_file():
                asset_map = {snapshot.identity.portrait.api_asset_id: local_portrait}

        raw_spec = kwargs.get("landing_page_spec")
        landing_page_spec: LandingPageSpec | None = None
        if isinstance(raw_spec, LandingPageSpec):
            landing_page_spec = raw_spec
        elif isinstance(raw_spec, dict):
            landing_page_spec = LandingPageSpec.model_validate(raw_spec)
        elif (
            is_landing_page
            and snapshot.campaign.plan is not None
            and snapshot.campaign.plan.landing_page is not None
        ):
            landing_page_spec = snapshot.campaign.plan.landing_page

        _, proposal, _ = await self.evaluate(
            snapshot,
            html=html,
            mode=mode,
            channel=str(kwargs.get("channel", "email")),
            asset_map=asset_map,
            save_screenshot_path=kwargs.get("save_screenshot_path"),
            page_url=kwargs.get("page_url"),
            expected_url=kwargs.get("expected_url"),
            landing_page_spec=landing_page_spec,
            extra_blocked_domains=kwargs.get("extra_blocked_domains", ()),
            revision_cycle=int(revision_raw),
            render=bool(kwargs.get("render", True)),
            run_llm=bool(kwargs.get("run_llm", True)),
        )
        return proposal


async def run_qa_loop[T](
    qa_agent: QAAgent,
    snapshot: Snapshot,
    produce: Callable[[QAVerdict | None, int], Awaitable[tuple[T, str]]],
    *,
    mode: RenderMode = "email",
    channel: str = "email",
    asset_map: dict[str, Path] | None = None,
    page_url: str | None = None,
    expected_url: str | None = None,
    landing_page_spec: LandingPageSpec | None = None,
    extra_blocked_domains: Iterable[str] = (),
    render: bool = True,
    run_llm: bool = True,
) -> QALoopResult:
    """Iterate an artifact producer against ``QAAgent`` up to ``qa_agent.max_revisions`` cycles."""
    last_verdict: QAVerdict | None = None
    last_proposal: Proposal | None = None
    last_artifact: Any = None
    last_render: RenderResult | None = None

    for cycle in range(qa_agent.max_revisions + 1):
        artifact, html = await produce(last_verdict, cycle)
        verdict, proposal, render_result = await qa_agent.evaluate(
            snapshot,
            html=html,
            mode=mode,
            channel=channel,
            asset_map=asset_map,
            page_url=page_url,
            expected_url=expected_url,
            landing_page_spec=landing_page_spec,
            extra_blocked_domains=extra_blocked_domains,
            revision_cycle=cycle,
            render=render,
            run_llm=run_llm,
        )
        last_artifact = artifact
        last_verdict = verdict
        last_proposal = proposal
        last_render = render_result
        if verdict.passed:
            return QALoopResult(
                artifact=artifact,
                verdict=verdict,
                proposal=proposal,
                cycles_used=cycle + 1,
                render_result=render_result,
            )

    assert last_verdict is not None and last_proposal is not None
    return QALoopResult(
        artifact=last_artifact,
        verdict=last_verdict,
        proposal=last_proposal,
        cycles_used=qa_agent.max_revisions + 1,
        render_result=last_render,
    )
