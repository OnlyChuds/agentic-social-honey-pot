from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import BaseModel

from src.agents.base import BaseAgent, load_prompt
from src.config import load_config
from src.llm.image import (
    DEFAULT_HQ_MODEL,
    DEFAULT_REALISM_LORA,
    DEFAULT_TURBO_MODEL,
    IMAGE_TOOL_SCHEMAS,
    ImageClient,
    build_edit_graph,
    build_portrait_graph,
    build_turbo_graph,
    make_image_tool_handlers,
)
from src.llm.text import TextCompletion, TextLLMClient, extract_json_text
from src.models import (
    BrandingManifest,
    CampaignPlan,
    DecisionRecord,
    Event,
    Fact,
    Hook,
    Profile,
    Proposal,
    QAVerdict,
    Snapshot,
)


def test_load_config_resolves_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "Qwen3.8-27B-abliterated")
    monkeypatch.setenv("IMAGE_BASE_URL", "https://img.example")
    monkeypatch.setenv("IMAGE_API_KEY", "")
    monkeypatch.setenv("IMAGE_MODEL", "qwen_image_2.1_bf16.safetensors")
    monkeypatch.setenv("TARGET_API_URL", "https://target.example/v1")
    monkeypatch.setenv("TEAM_API_KEY", "team-secret")

    cfg = load_config("config.yaml", env_path=None)
    assert cfg.models.text.base_url == "https://llm.example/v1"
    assert cfg.models.text.api_key == "sk-test"
    assert cfg.models.text.model == "Qwen3.8-27B-abliterated"
    assert cfg.models.image.base_url == "https://img.example"
    assert cfg.models.image.api_key == ""
    assert cfg.target_api.team_key == "team-secret"
    assert cfg.target_api.rate.req_per_min == 50


def test_all_prompts_load_cleanly() -> None:
    prompt_dir = Path("prompts")
    names = sorted(p.stem for p in prompt_dir.glob("*.md"))
    assert len(names) >= 5
    for name in names:
        spec = load_prompt(name)
        assert spec.system_prompt
        assert 0.0 <= spec.temperature <= 2.0
        assert spec.max_tokens > 0


def test_models_roundtrip() -> None:
    snap = Snapshot(
        version=3,
        facts=[Fact(claim="spoke at cloud conf", category="talks", confidence=0.9, osint_grade="A")],
        hooks=[Hook(topic="GPU cost", evidence=["f_1"], confidence=0.8)],
        profile=Profile(style_guide="concise"),
        identity=BrandingManifest(sender_name="Lena Vogt", domain="stackit-cloud-svc.com"),
    )
    snap2 = Snapshot.model_validate_json(snap.model_dump_json())
    assert snap2.version == 3
    assert snap2.facts[0].osint_grade == "A"
    assert snap2.identity is not None and snap2.identity.sender_name == "Lena Vogt"

    verdict = QAVerdict.model_validate({"pass": True, "issues": [], "required_fixes": []})
    assert verdict.passed is True
    assert verdict.model_dump(by_alias=True)["pass"] is True

    _ = Event(type="system", actor="orchestrator")
    _ = Proposal(agent="osint", kind="dossier", base_version=3)
    _ = CampaignPlan(objective="credentials", primary_hook="h_1")
    _ = DecisionRecord(trigger="boot", snapshot_version=3)


def test_extract_json_text() -> None:
    raw = '<think>reasoning here</think>\n```json\n{"ok": true, "n": 2}\n```'
    assert json.loads(extract_json_text(raw)) == {"ok": True, "n": 2}
    assert json.loads(extract_json_text('Here it is: {"a": 1} thanks')) == {"a": 1}


class _DemoPayload(BaseModel):
    answer: str
    score: int


class _DummyAgent(BaseAgent):
    agent_name = "identity"
    proposal_kind = "identity"


@pytest.mark.asyncio
async def test_text_client_json_retry_and_tool_loop() -> None:
    llm = TextLLMClient(base_url="https://llm.example/v1", api_key="k")

    # 1) complete_json recovers after invalid first attempt
    responses = [
        TextCompletion(content="not json at all"),
        TextCompletion(content='```json\n{"answer": "ok", "score": 9}\n```'),
    ]
    llm.complete = AsyncMock(side_effect=responses)  # type: ignore[method-assign]
    out = await llm.complete_json([{"role": "user", "content": "hi"}], _DemoPayload, retries=2)
    assert out == _DemoPayload(answer="ok", score=9)
    assert llm.complete.call_count == 2

    # 2) run_tool_loop executes tool call and feeds result back
    tool_responses = [
        TextCompletion(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "double", "arguments": '{"x": 21}'},
                }
            ],
        ),
        TextCompletion(content="The result is 42."),
    ]
    llm.complete = AsyncMock(side_effect=tool_responses)  # type: ignore[method-assign]
    loop_res = await llm.run_tool_loop(
        [{"role": "user", "content": "double 21"}],
        tools=[{"type": "function", "function": {"name": "double", "parameters": {}}}],
        handlers={"double": lambda args: {"y": args["x"] * 2}},
    )
    assert loop_res.final_content == "The result is 42."
    assert len(loop_res.tool_history) == 1
    assert json.loads(loop_res.tool_history[0].result) == {"y": 42}

    # 3) BaseAgent wraps payload into Proposal with snapshot version
    agent = _DummyAgent(llm)
    prop = agent.make_proposal(Snapshot(version=7), out, rationale="test")
    assert prop.base_version == 7
    assert prop.agent == "identity"
    assert prop.payload == {"answer": "ok", "score": 9}
    await llm.aclose()


def test_text_client_openai_and_vllm_request_kwargs() -> None:
    msgs = [{"role": "user", "content": "hi"}]

    qwen = TextLLMClient(
        base_url="https://vllm.example/v1",
        api_key="k",
        model="Qwen3.8-27B-abliterated",
    )
    qwen_kw = qwen._build_completion_kwargs(msgs, temperature=0.2, max_tokens=2048)
    assert qwen_kw["max_tokens"] == 2048
    assert qwen_kw["temperature"] == 0.2
    assert qwen_kw["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}

    gpt6 = TextLLMClient(
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        model="gpt-6",
    )
    gpt6_kw = gpt6._build_completion_kwargs(msgs, temperature=0.2, max_tokens=2048, json_mode=True)
    assert gpt6_kw["max_completion_tokens"] == 8192
    assert "max_tokens" not in gpt6_kw
    assert "temperature" not in gpt6_kw
    assert "extra_body" not in gpt6_kw
    assert gpt6_kw["response_format"] == {"type": "json_object"}

    gpt41 = TextLLMClient(
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        model="gpt-4.1",
    )
    gpt41_kw = gpt41._build_completion_kwargs(msgs, temperature=0.2, max_tokens=2048)
    assert gpt41_kw["max_completion_tokens"] == 2048
    assert gpt41_kw["temperature"] == 0.2
    assert "extra_body" not in gpt41_kw


def test_image_graph_builders() -> None:
    g_portrait = build_portrait_graph("casual photo of an engineer", seed=99)
    assert g_portrait["1"]["inputs"]["unet_name"] == DEFAULT_HQ_MODEL
    assert g_portrait["9"]["inputs"]["lora_name"] == DEFAULT_REALISM_LORA
    assert g_portrait["5"]["inputs"]["prompt"].startswith("lbxstyle, ")
    assert g_portrait["6"]["inputs"]["steps"] == 25
    assert g_portrait["6"]["inputs"]["cfg"] == 1.0

    g_turbo = build_turbo_graph("minimalist logo", seed=7)
    assert g_turbo["1"]["inputs"]["unet_name"] == DEFAULT_TURBO_MODEL
    assert g_turbo["9"]["class_type"] == "ManualSigmas"
    assert len(g_turbo["9"]["inputs"]["sigmas"].split(",")) == 9

    g_edit = build_edit_graph("ref.png", "change shirt to navy blazer", seed=11)
    assert g_edit["4"]["inputs"]["image"] == "ref.png"
    assert g_edit["5"]["inputs"]["images.image_1"] == ["4", 0]
    assert g_edit["10"]["inputs"]["latent_image"] == ["5", 2]


@pytest.mark.asyncio
async def test_image_client_and_tool_handlers(tmp_path: Path) -> None:
    fake_png = b"\x89PNG\r\n\x1a\nfake-image-bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "uploaded_ref.png"})
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "pid_123"})
        if request.url.path == "/history/pid_123":
            return httpx.Response(
                200,
                json={
                    "pid_123": {
                        "status": {"status_str": "success"},
                        "outputs": {
                            "8": {
                                "images": [
                                    {
                                        "filename": "out.png",
                                        "subfolder": "",
                                        "type": "output",
                                        "metadata": {"width": 1024, "height": 1024},
                                    }
                                ]
                            },
                            "12": {
                                "images": [
                                    {
                                        "filename": "out.png",
                                        "subfolder": "",
                                        "type": "output",
                                        "metadata": {"width": 1024, "height": 1024},
                                    }
                                ]
                            },
                        },
                    }
                },
            )
        if request.url.path == "/view":
            return httpx.Response(200, content=fake_png)
        return httpx.Response(404)

    mock_http = httpx.AsyncClient(
        base_url="https://comfy.test",
        transport=httpx.MockTransport(handler),
    )
    async with ImageClient("https://comfy.test", poll_interval_s=0.01, client=mock_http) as img:
        handlers = make_image_tool_handlers(img, output_dir=tmp_path)
        assert {s["function"]["name"] for s in IMAGE_TOOL_SCHEMAS} == set(handlers.keys())

        res1: dict[str, Any] = await handlers["generate_portrait"](
            {"prompt": "office manager near window", "filename": "lena.png", "seed": 12}
        )
        assert Path(res1["local_path"]).read_bytes() == fake_png

        res2: dict[str, Any] = await handlers["edit_image"](
            {
                "reference_path": res1["local_path"],
                "instruction": "outdoor candid lighting",
                "filename": "lena_outdoor.png",
                "seed": 13,
            }
        )
        assert Path(res2["local_path"]).read_bytes() == fake_png

        _ = MagicMock()
