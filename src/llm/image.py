from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import httpx

from src.config import ModelEndpointConfig
from src.llm.text import ToolHandler

TURBO_SIGMAS = "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0"
DEFAULT_HQ_MODEL = "qwen_image_2.1_bf16.safetensors"
DEFAULT_TURBO_MODEL = "qwen_image_2.1_turbo_bf16.safetensors"
DEFAULT_TEXT_ENCODER = "qwen3vl_8b_bf16.safetensors"
DEFAULT_VAE = "qwen_image_2.1_vae_bf16.safetensors"
DEFAULT_REALISM_LORA = "casual_phone_photo_lbxstyle.safetensors"
REALISM_LORA_TRIGGER = "lbxstyle"
MAX_ASSET_BYTES = 5 * 1024 * 1024  # Target API 5 MB per-asset limit


@dataclass(slots=True)
class GeneratedImage:
    png_bytes: bytes
    filename: str
    prompt_id: str
    seed: int
    width: int | None = None
    height: int | None = None
    local_path: Path | None = None


def _ensure_trigger(prompt: str, trigger: str = REALISM_LORA_TRIGGER) -> str:
    if trigger.lower() in prompt.lower():
        return prompt
    return f"{trigger}, {prompt}"


def build_portrait_graph(
    prompt: str,
    *,
    seed: int = 42,
    width: int = 1024,
    height: int = 1024,
    steps: int = 25,
    realistic_lora: bool = True,
    lora_name: str = DEFAULT_REALISM_LORA,
    lora_strength: float = 0.75,
    unet_name: str = DEFAULT_HQ_MODEL,
    filename_prefix: str = "persona_portrait",
) -> dict[str, Any]:
    """Build the ComfyUI API graph for 25-step Qwen-Image-2.1 HQ portraits (with optional realism LoRA)."""
    final_prompt = _ensure_trigger(prompt) if realistic_lora and lora_name == DEFAULT_REALISM_LORA else prompt
    model_ref: list[Any] = ["9", 0] if realistic_lora else ["1", 0]

    graph: dict[str, Any] = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": unet_name, "weight_dtype": "default"},
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": DEFAULT_TEXT_ENCODER, "type": "qwen_image", "device": "default"},
        },
        "3": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": DEFAULT_VAE},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "5": {
            "class_type": "TextEncodeQwenImage21",
            "inputs": {
                "clip": ["2", 0],
                "prompt": final_prompt,
                "negative_prompt": "",
                "resolution": max(width, height),
            },
        },
        "6": {
            "class_type": "KSampler",
            "inputs": {
                "model": model_ref,
                "positive": ["5", 0],
                "negative": ["5", 1],
                "latent_image": ["4", 0],
                "seed": seed,
                "steps": steps,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
            },
        },
        "7": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["6", 0], "vae": ["3", 0]},
        },
        "8": {
            "class_type": "SaveImage",
            "inputs": {"images": ["7", 0], "filename_prefix": filename_prefix},
        },
    }
    if realistic_lora:
        graph["9"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["1", 0],
                "lora_name": lora_name,
                "strength_model": lora_strength,
            },
        }
    return graph


def build_turbo_graph(
    prompt: str,
    *,
    seed: int = 42,
    width: int = 1024,
    height: int = 1024,
    unet_name: str = DEFAULT_TURBO_MODEL,
    filename_prefix: str = "qwen21_turbo",
) -> dict[str, Any]:
    """Build the ComfyUI API graph for fast 8-step Qwen-Image-2.1 Turbo T2I (logos, graphics, quick iterations)."""
    return {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": unet_name, "weight_dtype": "default"},
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": DEFAULT_TEXT_ENCODER, "type": "qwen_image", "device": "default"},
        },
        "3": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": DEFAULT_VAE},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "5": {
            "class_type": "TextEncodeQwenImage21",
            "inputs": {
                "clip": ["2", 0],
                "prompt": prompt,
                "negative_prompt": "",
                "resolution": max(width, height),
            },
        },
        "6": {
            "class_type": "CFGGuider",
            "inputs": {"model": ["1", 0], "positive": ["5", 0], "negative": ["5", 1], "cfg": 1.0},
        },
        "7": {
            "class_type": "RandomNoise",
            "inputs": {"noise_seed": seed},
        },
        "8": {
            "class_type": "KSamplerSelect",
            "inputs": {"sampler_name": "euler"},
        },
        "9": {
            "class_type": "ManualSigmas",
            "inputs": {"sigmas": TURBO_SIGMAS},
        },
        "10": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["7", 0],
                "guider": ["6", 0],
                "sampler": ["8", 0],
                "sigmas": ["9", 0],
                "latent_image": ["4", 0],
            },
        },
        "11": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["10", 0], "vae": ["3", 0]},
        },
        "12": {
            "class_type": "SaveImage",
            "inputs": {"images": ["11", 0], "filename_prefix": filename_prefix},
        },
    }


def build_edit_graph(
    uploaded_image_name: str,
    instruction: str,
    *,
    seed: int = 42,
    resolution: int = 1024,
    unet_name: str = DEFAULT_TURBO_MODEL,
    filename_prefix: str = "qwen21_edit",
) -> dict[str, Any]:
    """Build the ComfyUI API graph for 8-step Qwen-Image-2.1 Turbo reference-guided editing."""
    return {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": unet_name, "weight_dtype": "default"},
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": DEFAULT_TEXT_ENCODER, "type": "qwen_image", "device": "default"},
        },
        "3": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": DEFAULT_VAE},
        },
        "4": {
            "class_type": "LoadImage",
            "inputs": {"image": uploaded_image_name},
        },
        "5": {
            "class_type": "TextEncodeQwenImage21",
            "inputs": {
                "clip": ["2", 0],
                "vae": ["3", 0],
                "prompt": instruction,
                "negative_prompt": "",
                "resolution": resolution,
                "images.image_1": ["4", 0],
            },
        },
        "6": {
            "class_type": "CFGGuider",
            "inputs": {"model": ["1", 0], "positive": ["5", 0], "negative": ["5", 1], "cfg": 1.0},
        },
        "7": {
            "class_type": "RandomNoise",
            "inputs": {"noise_seed": seed},
        },
        "8": {
            "class_type": "KSamplerSelect",
            "inputs": {"sampler_name": "euler"},
        },
        "9": {
            "class_type": "ManualSigmas",
            "inputs": {"sigmas": TURBO_SIGMAS},
        },
        "10": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["7", 0],
                "guider": ["6", 0],
                "sampler": ["8", 0],
                "sigmas": ["9", 0],
                "latent_image": ["5", 2],
            },
        },
        "11": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["10", 0], "vae": ["3", 0]},
        },
        "12": {
            "class_type": "SaveImage",
            "inputs": {"images": ["11", 0], "filename_prefix": filename_prefix},
        },
    }


class ImageClient:
    """Async ComfyUI REST client for Qwen-Image-2.1 (BF16 + Turbo + Realism LoRA)."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout_s: float = 180.0,
        poll_interval_s: float = 1.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0),
        )

    @classmethod
    def from_config(cls, cfg: ModelEndpointConfig) -> ImageClient:
        return cls(base_url=cfg.base_url, api_key=cfg.api_key)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def upload_image(
        self,
        image: Path | bytes,
        filename: str = "reference.png",
    ) -> str:
        """Upload a reference image to ComfyUI's input directory and return its stored name."""
        if isinstance(image, Path):
            data = image.read_bytes()
            filename = image.name
        else:
            data = image
        resp = await self._client.post(
            "/upload/image",
            files={"image": (filename, data, "image/png")},
            data={"overwrite": "true"},
        )
        resp.raise_for_status()
        payload = resp.json()
        return str(payload["name"])

    async def _run_graph(
        self,
        graph: dict[str, Any],
        save_node_id: str,
        *,
        seed: int,
        save_path: Path | None = None,
    ) -> GeneratedImage:
        resp = await self._client.post("/prompt", json={"prompt": graph})
        resp.raise_for_status()
        prompt_id = str(resp.json()["prompt_id"])

        deadline = asyncio.get_running_loop().time() + self.timeout_s
        while True:
            hist_resp = await self._client.get(f"/history/{prompt_id}")
            hist_resp.raise_for_status()
            hist = hist_resp.json()
            if prompt_id in hist:
                entry = hist[prompt_id]
                status = entry.get("status", {})
                if status.get("status_str") != "success":
                    raise RuntimeError(f"ComfyUI execution failed: {status}")
                images = entry["outputs"][save_node_id]["images"]
                img_info = images[0]
                filename = str(img_info["filename"])
                subfolder = str(img_info.get("subfolder", ""))
                img_type = str(img_info.get("type", "output"))
                meta = img_info.get("metadata") or {}

                dl = await self._client.get(
                    "/view",
                    params={"filename": filename, "subfolder": subfolder, "type": img_type},
                )
                dl.raise_for_status()
                png_bytes = dl.content
                if len(png_bytes) > MAX_ASSET_BYTES:
                    raise ValueError(
                        f"Generated image ({len(png_bytes)} bytes) exceeds 5 MB Target API limit"
                    )

                out_path: Path | None = None
                if save_path is not None:
                    save_path.parent.mkdir(parents=True, exist_ok=True)
                    save_path.write_bytes(png_bytes)
                    out_path = save_path

                return GeneratedImage(
                    png_bytes=png_bytes,
                    filename=filename,
                    prompt_id=prompt_id,
                    seed=seed,
                    width=meta.get("width"),
                    height=meta.get("height"),
                    local_path=out_path,
                )

            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    f"ComfyUI prompt {prompt_id} did not finish within {self.timeout_s}s"
                )
            await asyncio.sleep(self.poll_interval_s)

    async def generate_portrait(
        self,
        prompt: str,
        *,
        seed: int | None = None,
        width: int = 1024,
        height: int = 1024,
        realistic_lora: bool = True,
        lora_strength: float = 0.75,
        steps: int = 25,
        save_path: Path | None = None,
    ) -> GeneratedImage:
        """Generate a realistic human portrait (defaults to 25-step BF16 + casual phone LoRA)."""
        use_seed = seed if seed is not None else random.randint(1, 2**31 - 1)
        graph = build_portrait_graph(
            prompt,
            seed=use_seed,
            width=width,
            height=height,
            steps=steps,
            realistic_lora=realistic_lora,
            lora_strength=lora_strength,
        )
        return await self._run_graph(graph, save_node_id="8", seed=use_seed, save_path=save_path)

    async def generate_fast(
        self,
        prompt: str,
        *,
        seed: int | None = None,
        width: int = 1024,
        height: int = 1024,
        save_path: Path | None = None,
    ) -> GeneratedImage:
        """Generate an image quickly in 8 steps via Qwen-Image-2.1 Turbo (logos, graphics, fast drafts)."""
        use_seed = seed if seed is not None else random.randint(1, 2**31 - 1)
        graph = build_turbo_graph(prompt, seed=use_seed, width=width, height=height)
        return await self._run_graph(graph, save_node_id="12", seed=use_seed, save_path=save_path)

    async def edit_image(
        self,
        reference_image: Path | bytes,
        instruction: str,
        *,
        seed: int | None = None,
        resolution: int = 1024,
        save_path: Path | None = None,
    ) -> GeneratedImage:
        """Edit or restyle an existing portrait/logo while preserving subject identity (8-step Turbo)."""
        use_seed = seed if seed is not None else random.randint(1, 2**31 - 1)
        uploaded_name = await self.upload_image(reference_image)
        graph = build_edit_graph(
            uploaded_name,
            instruction,
            seed=use_seed,
            resolution=resolution,
        )
        return await self._run_graph(graph, save_node_id="12", seed=use_seed, save_path=save_path)


IMAGE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "generate_portrait",
            "description": (
                "Generate a realistic, non-AI-looking candid photo or headshot of a persona "
                "using Qwen-Image-2.1 BF16 with the casual phone photo realism LoRA. "
                "Avoid glossy buzzwords (studio lighting, editorial, 85mm, bokeh); describe "
                "everyday lighting, real clothing, and natural unretouched skin."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed candid photo description."},
                    "filename": {
                        "type": "string",
                        "description": "Output PNG filename (e.g. 'lena_vogt.png').",
                    },
                    "seed": {"type": "integer", "description": "Fixed seed for reproducibility."},
                    "lora_strength": {
                        "type": "number",
                        "description": "Realism LoRA strength (0.65-0.85 recommended, default 0.75).",
                    },
                },
                "required": ["prompt", "filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_fast_image",
            "description": (
                "Fast 8-step Qwen-Image-2.1 Turbo generation for logos, badges, visual graphics, "
                "or transparent RGBA assets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Image or logo prompt."},
                    "filename": {"type": "string", "description": "Output PNG filename."},
                    "width": {"type": "integer", "default": 1024},
                    "height": {"type": "integer", "default": 1024},
                    "seed": {"type": "integer"},
                },
                "required": ["prompt", "filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_image",
            "description": (
                "Edit or restyle an existing local image (e.g. create a second photo of the same "
                "persona in a different setting without face drift, or remove a background)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reference_path": {
                        "type": "string",
                        "description": "Path to existing local image file.",
                    },
                    "instruction": {"type": "string", "description": "What to edit or change."},
                    "filename": {"type": "string", "description": "Output PNG filename."},
                    "seed": {"type": "integer"},
                },
                "required": ["reference_path", "instruction", "filename"],
            },
        },
    },
]


def make_image_tool_handlers(
    client: ImageClient,
    output_dir: Path = Path("assets"),
) -> dict[str, ToolHandler]:
    """Bind IMAGE_TOOL_SCHEMAS to async handler callables that save PNGs under output_dir."""

    def _safe_out(name: str) -> Path:
        clean = Path(name).name
        if not clean.lower().endswith(".png"):
            clean += ".png"
        return output_dir / clean

    async def _gen_portrait(args: dict[str, Any]) -> dict[str, Any]:
        out = _safe_out(str(args["filename"]))
        res = await client.generate_portrait(
            prompt=str(args["prompt"]),
            seed=int(args["seed"]) if args.get("seed") is not None else None,
            lora_strength=float(args.get("lora_strength", 0.75)),
            save_path=out,
        )
        return {
            "local_path": str(res.local_path),
            "bytes": len(res.png_bytes),
            "seed": res.seed,
            "width": res.width,
            "height": res.height,
        }

    async def _gen_fast(args: dict[str, Any]) -> dict[str, Any]:
        out = _safe_out(str(args["filename"]))
        res = await client.generate_fast(
            prompt=str(args["prompt"]),
            seed=int(args["seed"]) if args.get("seed") is not None else None,
            width=int(args.get("width", 1024)),
            height=int(args.get("height", 1024)),
            save_path=out,
        )
        return {
            "local_path": str(res.local_path),
            "bytes": len(res.png_bytes),
            "seed": res.seed,
            "width": res.width,
            "height": res.height,
        }

    async def _edit(args: dict[str, Any]) -> dict[str, Any]:
        ref = Path(str(args["reference_path"]))
        if not ref.exists():
            ref = output_dir / ref.name
        out = _safe_out(str(args["filename"]))
        res = await client.edit_image(
            reference_image=ref,
            instruction=str(args["instruction"]),
            seed=int(args["seed"]) if args.get("seed") is not None else None,
            save_path=out,
        )
        return {
            "local_path": str(res.local_path),
            "bytes": len(res.png_bytes),
            "seed": res.seed,
            "width": res.width,
            "height": res.height,
        }

    return {
        "generate_portrait": _gen_portrait,
        "generate_fast_image": _gen_fast,
        "edit_image": _edit,
    }
