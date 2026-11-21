from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Self, TypeVar, cast

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam
from pydantic import BaseModel, ValidationError

from src.config import ModelEndpointConfig

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
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": cast(list[ChatCompletionMessageParam], messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": enable_thinking}},
        }
        if tools:
            kwargs["tools"] = cast(list[ChatCompletionToolParam], tools)
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        resp = await self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message
        raw_tool_calls: list[dict[str, Any]] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                fn = getattr(tc, "function", None)
                if fn is not None:
                    raw_tool_calls.append(
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": fn.name,
                                "arguments": fn.arguments,
                            },
                        }
                    )

        reasoning = getattr(msg, "reasoning", None) or getattr(msg, "reasoning_content", None)
        usage = resp.usage
        return TextCompletion(
            content=msg.content or "",
            reasoning=reasoning if isinstance(reasoning, str) else None,
            tool_calls=raw_tool_calls,
            finish_reason=choice.finish_reason,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )

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
