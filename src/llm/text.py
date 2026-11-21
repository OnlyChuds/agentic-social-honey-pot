from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Self, TypeVar, cast

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    NotFoundError,
    RateLimitError,
)
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam
from pydantic import BaseModel, ValidationError

from src.config import ModelEndpointConfig

_TRANSIENT_LLM_ERRORS = (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    NotFoundError,
    RateLimitError,
)

T = TypeVar("T", bound=BaseModel)
ToolHandler = Callable[[dict[str, Any]], Any | Awaitable[Any]]

_THINK_BLOCK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def extract_json_text(raw: str) -> str:
    """Extract a JSON object/array string from LLM output (handles <think> tags and code fences)."""
    cleaned = _THINK_BLOCK_RE.sub("", raw).strip()
    fence_match = _CODE_FENCE_RE.search(cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    # If surrounding prose exists, slice between outermost {} or []
    start_obj = cleaned.find("{")
    start_arr = cleaned.find("[")
    starts = [i for i in (start_obj, start_arr) if i != -1]
    if not starts:
        return cleaned
    start = min(starts)
    open_ch = cleaned[start]
    close_ch = "}" if open_ch == "{" else "]"
    end = cleaned.rfind(close_ch)
    if end != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


@dataclass(slots=True)
class ToolCallRecord:
    name: str
    arguments: dict[str, Any]
    result: str


@dataclass(slots=True)
class TextCompletion:
    content: str
    reasoning: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(slots=True)
class ToolLoopResult:
    final_content: str
    reasoning: str | None
    messages: list[dict[str, Any]]
    tool_history: list[ToolCallRecord]
    rounds: int


class TextLLMClient:
    """Async OpenAI-compatible client for self-hosted Qwen3.8-27B-abliterated (vLLM)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "Qwen3.8-27B-abliterated",
        timeout_s: float = 120.0,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = client or AsyncOpenAI(
            base_url=self.base_url,
            api_key=api_key or "EMPTY",
            timeout=timeout_s,
        )

    @classmethod
    def from_config(cls, cfg: ModelEndpointConfig, timeout_s: float = 120.0) -> TextLLMClient:
        return cls(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=cfg.model or "Qwen3.8-27B-abliterated",
            timeout_s=timeout_s,
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def _is_openai_hosted(self) -> bool:
        url = self.base_url.lower()
        return "api.openai.com" in url or "openai.azure.com" in url

    def _is_openai_reasoning_or_nextgen_model(self) -> bool:
        model = self.model.strip().lower()
        # Handle optional provider prefix like "openai/gpt-6"
        if "/" in model:
            model = model.rsplit("/", 1)[-1]
        return model.startswith(("o1", "o3", "o4", "gpt-5", "gpt-6"))

    def _is_vllm_qwen_model(self) -> bool:
        if self._is_openai_hosted():
            return False
        model = self.model.strip().lower()
        if "/" in model:
            model = model.rsplit("/", 1)[-1]
        return not model.startswith(("gpt-", "o1", "o3", "o4", "claude-", "gemini-"))

    def _build_completion_kwargs(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        enable_thinking: bool = False,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        json_mode: bool = False,
    ) -> dict[str, Any]:
        is_openai = self._is_openai_hosted()
        is_nextgen = self._is_openai_reasoning_or_nextgen_model()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": cast(list[ChatCompletionMessageParam], messages),
        }
        if is_openai or is_nextgen:
            kwargs["max_completion_tokens"] = max(max_tokens, 8192) if is_nextgen else max_tokens
            if not is_nextgen:
                kwargs["temperature"] = temperature
        else:
            kwargs["temperature"] = temperature
            kwargs["max_tokens"] = max_tokens
            if self._is_vllm_qwen_model():
                kwargs["extra_body"] = {
                    "chat_template_kwargs": {"enable_thinking": enable_thinking}
                }
        if tools:
            kwargs["tools"] = cast(list[ChatCompletionToolParam], tools)
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}
        return kwargs

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        enable_thinking: bool = False,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        json_mode: bool = False,
    ) -> TextCompletion:
        kwargs = self._build_completion_kwargs(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            enable_thinking=enable_thinking,
            tools=tools,
            tool_choice=tool_choice,
            json_mode=json_mode,
        )

        backoff_delays = (1.5, 3.5)
        for attempt in range(len(backoff_delays) + 1):
            try:
                stream = await self._client.chat.completions.create(**kwargs)
                content_parts: list[str] = []
                reasoning_parts: list[str] = []
                tc_acc: dict[int, dict[str, Any]] = {}
                finish_reason: str | None = None
                prompt_tokens = 0
                completion_tokens = 0

                async for chunk in stream:
                    if chunk.usage is not None:
                        prompt_tokens = chunk.usage.prompt_tokens
                        completion_tokens = chunk.usage.completion_tokens
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    if choice.finish_reason is not None:
                        finish_reason = choice.finish_reason
                    delta = choice.delta
                    if delta.content:
                        content_parts.append(delta.content)
                    r_chunk = getattr(delta, "reasoning", None) or getattr(
                        delta, "reasoning_content", None
                    )
                    if isinstance(r_chunk, str) and r_chunk:
                        reasoning_parts.append(r_chunk)
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            slot = tc_acc.setdefault(
                                idx,
                                {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                },
                            )
                            if tc.id:
                                slot["id"] = tc.id
                            if tc.function is not None:
                                if tc.function.name:
                                    slot["function"]["name"] += tc.function.name
                                if tc.function.arguments:
                                    slot["function"]["arguments"] += tc.function.arguments

                raw_tool_calls = [tc_acc[i] for i in sorted(tc_acc)]
                return TextCompletion(
                    content="".join(content_parts),
                    reasoning="".join(reasoning_parts) or None,
                    tool_calls=raw_tool_calls,
                    finish_reason=finish_reason,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            except _TRANSIENT_LLM_ERRORS:
                if attempt >= len(backoff_delays):
                    raise
                await asyncio.sleep(backoff_delays[attempt])
        raise RuntimeError("unreachable")

    async def complete_json(
        self,
        messages: list[dict[str, Any]],
        schema_model: type[T],
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        enable_thinking: bool = False,
        retries: int = 2,
    ) -> T:
        """Call the LLM and validate the JSON response against a Pydantic model, retrying on errors."""
        convo = [dict(m) for m in messages]
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            comp = await self.complete(
                convo,
                temperature=temperature,
                max_tokens=max_tokens,
                enable_thinking=enable_thinking,
                json_mode=True,
            )
            extracted = extract_json_text(comp.content)
            try:
                data = json.loads(extracted)
                return schema_model.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                if attempt >= retries:
                    break
                convo.append({"role": "assistant", "content": comp.content})
                convo.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response did not match the required JSON schema.\n"
                            f"Error: {exc}\n"
                            "Return ONLY a single valid JSON object matching the schema."
                        ),
                    }
                )

        raise ValueError(
            f"Failed to produce valid {schema_model.__name__} after {retries + 1} attempts: "
            f"{last_error}"
        )

    async def run_tool_loop(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        handlers: dict[str, ToolHandler],
        *,
        max_rounds: int = 8,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        enable_thinking: bool = False,
    ) -> ToolLoopResult:
        """Execute an OpenAI-style tool-calling loop until the model returns a final text reply."""
        convo: list[dict[str, Any]] = [dict(m) for m in messages]
        history: list[ToolCallRecord] = []
        last_reasoning: str | None = None

        for round_idx in range(1, max_rounds + 1):
            comp = await self.complete(
                convo,
                temperature=temperature,
                max_tokens=max_tokens,
                enable_thinking=enable_thinking,
                tools=tools,
                tool_choice="auto",
            )
            last_reasoning = comp.reasoning

            if not comp.tool_calls:
                convo.append({"role": "assistant", "content": comp.content})
                return ToolLoopResult(
                    final_content=comp.content,
                    reasoning=last_reasoning,
                    messages=convo,
                    tool_history=history,
                    rounds=round_idx,
                )

            convo.append(
                {
                    "role": "assistant",
                    "content": comp.content or "",
                    "tool_calls": comp.tool_calls,
                }
            )

            for tc in comp.tool_calls:
                fn_name = str(tc["function"]["name"])
                raw_args = str(tc["function"]["arguments"] or "{}")
                try:
                    parsed_args = json.loads(raw_args)
                    if not isinstance(parsed_args, dict):
                        parsed_args = {"value": parsed_args}
                except json.JSONDecodeError:
                    parsed_args = {}

                handler = handlers.get(fn_name)
                if handler is None:
                    result_str = json.dumps({"error": f"Unknown tool: {fn_name}"})
                else:
                    try:
                        out = handler(parsed_args)
                        if inspect.isawaitable(out):
                            out = await out
                        result_str = (
                            out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                        )
                    except Exception as exc:  # noqa: BLE001
                        result_str = json.dumps({"error": f"{type(exc).__name__}: {exc}"})

                history.append(
                    ToolCallRecord(name=fn_name, arguments=parsed_args, result=result_str)
                )
                convo.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": fn_name,
                        "content": result_str,
                    }
                )

        # Max rounds reached — ask for final synthesis without tools
        final = await self.complete(
            convo,
            temperature=temperature,
            max_tokens=max_tokens,
            enable_thinking=enable_thinking,
        )
        convo.append({"role": "assistant", "content": final.content})
        return ToolLoopResult(
            final_content=final.content,
            reasoning=final.reasoning or last_reasoning,
            messages=convo,
            tool_history=history,
            rounds=max_rounds,
        )
