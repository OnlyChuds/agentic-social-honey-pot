from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

from src.llm.text import TextLLMClient, ToolHandler, ToolLoopResult
from src.models import Actor, Proposal, ProposalKind, Snapshot

T = TypeVar("T", bound=BaseModel)


@dataclass(slots=True)
class PromptSpec:
    name: str
    system_prompt: str
    temperature: float = 0.3
    max_tokens: int = 4096
    metadata: dict[str, Any] = field(default_factory=dict)


def _parse_frontmatter(block: str) -> dict[str, Any]:
    """Parse YAML frontmatter, falling back to tolerant key: value lines if unquoted colons exist."""
    try:
        parsed = yaml.safe_load(block)
        if isinstance(parsed, dict):
            return parsed
    except yaml.YAMLError:
        pass

    meta: dict[str, Any] = {}
    last_key: str | None = None
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace() and last_key is not None:
            meta[last_key] = f"{meta[last_key]} {line.strip()}".strip()
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            key = k.strip()
            val = v.strip()
            meta[key] = val
            last_key = key
    return meta


def load_prompt(name: str, prompts_dir: Path = Path("prompts")) -> PromptSpec:
    """Load prompts/<name>.md, parsing optional YAML frontmatter for temperature/max_tokens."""
    path = prompts_dir / f"{name}.md"
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    body = raw

    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            meta = _parse_frontmatter(parts[1])
            body = parts[2].strip()

    return PromptSpec(
        name=str(meta.get("agent", name)),
        system_prompt=body,
        temperature=float(meta.get("temperature", 0.3)),
        max_tokens=int(meta.get("max_tokens", 4096)),
        metadata=meta,
    )


class BaseAgent:
    """Shared foundation for all agents: prompt loading, tool loop, JSON validation, Proposal emission."""

    agent_name: Actor
    proposal_kind: ProposalKind

    def __init__(
        self,
        llm: TextLLMClient,
        *,
        prompt_name: str | None = None,
        prompts_dir: Path = Path("prompts"),
    ) -> None:
        self.llm = llm
        self.prompt = load_prompt(prompt_name or self.agent_name, prompts_dir=prompts_dir)

    def build_messages(self, user_context: str | dict[str, Any]) -> list[dict[str, Any]]:
        content = (
            user_context
            if isinstance(user_context, str)
            else json.dumps(user_context, indent=2, ensure_ascii=False)
        )
        return [
            {"role": "system", "content": self.prompt.system_prompt},
            {"role": "user", "content": content},
        ]

    async def run_json(
        self,
        user_context: str | dict[str, Any],
        payload_model: type[T],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        enable_thinking: bool = False,
        retries: int = 2,
    ) -> T:
        return await self.llm.complete_json(
            self.build_messages(user_context),
            payload_model,
            temperature=self.prompt.temperature if temperature is None else temperature,
            max_tokens=self.prompt.max_tokens if max_tokens is None else max_tokens,
            enable_thinking=enable_thinking,
            retries=retries,
        )

    async def run_with_tools(
        self,
        user_context: str | dict[str, Any],
        tools: list[dict[str, Any]],
        handlers: dict[str, ToolHandler],
        *,
        max_rounds: int = 8,
        temperature: float | None = None,
        max_tokens: int | None = None,
        enable_thinking: bool = False,
    ) -> ToolLoopResult:
        return await self.llm.run_tool_loop(
            self.build_messages(user_context),
            tools=tools,
            handlers=handlers,
            max_rounds=max_rounds,
            temperature=self.prompt.temperature if temperature is None else temperature,
            max_tokens=self.prompt.max_tokens if max_tokens is None else max_tokens,
            enable_thinking=enable_thinking,
        )

    def make_proposal(
        self,
        snapshot: Snapshot,
        payload: BaseModel | dict[str, Any],
        *,
        kind: ProposalKind | None = None,
        rationale: str = "",
    ) -> Proposal:
        payload_dict = (
            payload.model_dump(mode="json", by_alias=True)
            if isinstance(payload, BaseModel)
            else payload
        )
        return Proposal(
            agent=self.agent_name,
            kind=kind or self.proposal_kind,
            base_version=snapshot.version,
            payload=payload_dict,
            rationale=rationale,
        )
