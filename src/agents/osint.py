from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.agents.base import BaseAgent
from src.config import AppConfig
from src.llm.text import TextLLMClient
from src.models import Actor, Fact, FactCategory, Hook, Proposal, ProposalKind, PsychTrait, Snapshot
from src.osint import OsintEngine, OsintResult, render_findings

_SYNTH_TIMEOUT_S = 110.0
_FALLBACK_TIMEOUT_S = 60.0

_GRADE_CONFIDENCE: dict[str, float] = {
    "A": 0.9,
    "B": 0.7,
    "C": 0.5,
    "D": 0.3,
}


class OrgBranding(BaseModel):
    real_domain: str = ""
    brand_keywords: list[str] = Field(default_factory=list)
    notes: str = ""


class DossierPayload(BaseModel):
    facts: list[Fact] = Field(default_factory=list)
    hook_seeds: list[Hook] = Field(default_factory=list)
    psychoprofile: list[PsychTrait] = Field(default_factory=list)
    org_branding: OrgBranding = Field(default_factory=OrgBranding)
    open_questions: list[str] = Field(default_factory=list)
    budget_spent_usd: float = 0.0
    escalation_level: int = Field(default=1, ge=1, le=4)
    completeness: Literal["full", "partial", "thin"] = "partial"


def _truncate(text: str, max_chars: int = 8000) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n... [truncated {len(text) - max_chars} chars]"


def _normalize_dossier(dossier: DossierPayload, result: OsintResult) -> DossierPayload:
    valid_fact_ids: set[str] = set()
    for fact in dossier.facts:
        if fact.osint_grade in _GRADE_CONFIDENCE and fact.confidence == 0.5:
            fact.confidence = _GRADE_CONFIDENCE[fact.osint_grade]
        valid_fact_ids.add(fact.fact_id)

    for hook in dossier.hook_seeds:
        hook.status = "candidate"
        if hook.evidence and valid_fact_ids:
            anchored = [fid for fid in hook.evidence if fid in valid_fact_ids]
            hook.evidence = anchored

    for trait in dossier.psychoprofile:
        if not trait.evidence and trait.confidence > 0.6:
            trait.confidence = 0.6

    if not dossier.org_branding.real_domain and result.org_domain:
        dossier.org_branding.real_domain = result.org_domain
    dossier.budget_spent_usd = round(result.budget_usd, 4)
    if any(s.kind == "linkedin_scrape" and s.ok for s in result.sources):
        dossier.escalation_level = 2
    return dossier


def _fallback_dossier(result: OsintResult, name: str, org: str) -> DossierPayload:
    """Best-effort dossier from raw sources when LLM synthesis fails or times out."""
    facts: list[Fact] = []
    for s in result.sources:
        if not s.ok or not s.text:
            continue
        claim = (s.title or s.label or s.text[:120]).strip().splitlines()[0][:200]
        if not claim:
            continue
        category: FactCategory = "social" if s.kind == "linkedin_scrape" else "other"
        facts.append(
            Fact(
                claim=f"{claim} ({s.url or s.label})",
                category=category,
                source=s.url or s.label,
                provenance="osint",
                confidence=0.6,
                osint_grade="B",
            )
        )
        if len(facts) >= 10:
            break
    ok_count = sum(1 for s in result.sources if s.ok and s.text)
    return DossierPayload(
        facts=facts,
        org_branding=OrgBranding(real_domain=result.org_domain),
        budget_spent_usd=round(result.budget_usd, 4),
        escalation_level=2 if any(s.kind == "linkedin_scrape" and s.ok for s in result.sources) else 1,
        completeness="partial" if ok_count >= 3 else "thin",
    )


class OSINTAgent(BaseAgent):
    """Research agent: parallel burst OSINT (src/osint) + one LLM call to synthesize a dossier Proposal."""

    agent_name: Actor = "osint"
    proposal_kind: ProposalKind = "dossier"

    def __init__(
        self,
        llm: TextLLMClient,
        *,
        config: AppConfig | None = None,
        engine: OsintEngine | None = None,
        prompts_dir: Path = Path("prompts"),
    ) -> None:
        super().__init__(llm, prompts_dir=prompts_dir)
        self.config = config
        self._injected_engine = engine
        self.last_result: OsintResult | None = None

    def _make_engine(self, scenario: str, max_budget_usd: float | None) -> OsintEngine:
        if self._injected_engine is not None:
            return self._injected_engine
        if max_budget_usd is not None:
            budget = max_budget_usd
        elif self.config is not None:
            budget = (
                self.config.budgets.osint_max_usd_eval
                if scenario == "eval"
                else self.config.budgets.osint_max_usd_practice
            )
        else:
            budget = 0.75 if scenario == "eval" else 2.0
        return OsintEngine.from_env(max_budget_usd=budget)

    async def run(
        self,
        snapshot: Snapshot,
        *,
        mode: str | None = None,
        focus_question: str | None = None,
        max_rounds: int = 8,
        max_budget_usd: float | None = None,
        **_extra: Any,
    ) -> Proposal:
        del max_rounds
        t_start = time.monotonic()
        target = snapshot.target
        engine = self._make_engine(snapshot.run.scenario, max_budget_usd)
        result = await engine.run(
            target.name,
            target.org,
            role=target.role,
            focus=focus_question or "",
        )
        self.last_result = result
        findings = _truncate(
            render_findings(
                result,
                name=target.name,
                org=target.org,
                role=target.role,
                focus=focus_question or "",
            ),
            10000,
        )

        exact_template = {
            "facts": [
                {
                    "fact_id": "f_01",
                    "claim": "One-sentence factual observation.",
                    "category": "role | projects | talks | company | tech | social | relationship | other",
                    "source": "https://exact-source-url",
                    "provenance": "osint",
                    "confidence": 0.9,
                    "osint_grade": "A | B | C | D",
                    "status": "active",
                }
            ],
            "hook_seeds": [
                {
                    "hook_id": "h_01",
                    "topic": "Specific actionable social-engineering leverage angle",
                    "evidence": ["f_01"],
                    "why_leverage": "Why target will engage with this topic",
                    "confidence": 0.8,
                    "status": "candidate",
                }
            ],
            "psychoprofile": [
                {"trait": "Concrete behavioral trait", "confidence": 0.6, "evidence": ["f_01"]}
            ],
            "org_branding": {
                "real_domain": "company.ai",
                "brand_keywords": ["keyword1", "keyword2"],
                "notes": "Brief org tone/branding notes",
            },
            "open_questions": ["Short gap to investigate later"],
            "budget_spent_usd": 0.03,
            "escalation_level": 1,
            "completeness": "full | partial | thin",
        }

        synthesis_messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.prompt.system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "target": target.model_dump(mode="json"),
                        "scenario": snapshot.run.scenario,
                        "mode": mode or "bootstrap",
                        "focus_question": focus_question,
                        "research_findings": findings,
                        "instructions": (
                            "Return ONLY compact JSON using the EXACT keys shown in exact_template "
                            "(use fact_id, claim, osint_grade — NOT id/statement/grade). "
                            "Emit 8-10 concise 1-sentence facts, 3-5 concrete hook_seeds anchored "
                            "to those fact_ids, 3-4 psychoprofile traits anchored to fact_ids, "
                            "org_branding, and at most 3 open_questions."
                        ),
                        "exact_template": exact_template,
                    },
                    ensure_ascii=False,
                ),
            },
        ]

        try:
            dossier = await asyncio.wait_for(
                self.llm.complete_json(
                    synthesis_messages,
                    DossierPayload,
                    temperature=0.2,
                    max_tokens=min(self.prompt.max_tokens, 2048),
                    retries=1,
                ),
                timeout=_SYNTH_TIMEOUT_S,
            )
        except (TimeoutError, ValueError):
            try:
                dossier = await asyncio.wait_for(
                    self.llm.complete_json(
                        synthesis_messages,
                        DossierPayload,
                        temperature=0.2,
                        max_tokens=min(self.prompt.max_tokens, 1536),
                        retries=0,
                    ),
                    timeout=_FALLBACK_TIMEOUT_S,
                )
            except (TimeoutError, ValueError):
                dossier = _fallback_dossier(result, target.name, target.org)
        dossier = _normalize_dossier(dossier, result)
        result.total_s = time.monotonic() - t_start

        ok_sources = sum(1 for s in result.sources if s.ok and s.text)
        rationale = (
            f"OSINT ({dossier.completeness}): {len(dossier.facts)} facts, "
            f"{len(dossier.hook_seeds)} hook seeds, ${dossier.budget_spent_usd:.3f} spent, "
            f"{ok_sources} sources, bursts {result.timings.get('bursts_s', 0.0):.1f}s, "
            f"total {result.total_s:.1f}s."
        )
        return self.make_proposal(snapshot, dossier, rationale=rationale)


# Alias expected by src/main.py discover_agents()
OsintAgent = OSINTAgent
