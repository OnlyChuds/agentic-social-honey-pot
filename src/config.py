from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_env(value: Any) -> Any:
    """Recursively expand ${VAR} placeholders using current environment variables."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_resolve_env(item) for item in value]
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    return value


class RunConfig(BaseModel):
    scenario: Literal["practice", "eval"] = "practice"
    target_id: str = "julius"
    run_no: int = 1
    data_dir: Path = Path("data")


class PacingConfig(BaseModel):
    min_gap_s: int = 600
    silence_check_s: int = 120


class BudgetsConfig(BaseModel):
    osint_max_usd_eval: float = 0.75
    osint_max_usd_practice: float = 2.0
    osint_escalation: Literal["auto", "capped_l2", "max"] = "auto"
    max_revisions: int = 2


class ModelEndpointConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


class ModelsConfig(BaseModel):
    text: ModelEndpointConfig = Field(default_factory=ModelEndpointConfig)
    image: ModelEndpointConfig = Field(default_factory=ModelEndpointConfig)


class RateConfig(BaseModel):
    req_per_min: int = 50
    max_parallel: int = 5


class RetryConfig(BaseModel):
    backoff_s: list[int] = Field(default_factory=lambda: [30, 60, 90])


class TargetApiConfig(BaseModel):
    base_url: str = "https://target-api.apps.01.cf.eu01.stackit.cloud/v1"
    team_key: str = ""
    rate: RateConfig = Field(default_factory=RateConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)


class ApifyActorsConfig(BaseModel):
    linkedin_profile: str = ""
    x_profile: str = ""
    instagram_profile: str = ""
    youtube_channel: str = ""


class ApifyConfig(BaseModel):
    actors: ApifyActorsConfig = Field(default_factory=ApifyActorsConfig)


class AggressivePacingConfig(BaseModel):
    min_gap_s: int = 300


class AggressiveBudgetConfig(BaseModel):
    compressed_plan_touches: int = 10


class AggressiveConfig(BaseModel):
    pacing: AggressivePacingConfig = Field(default_factory=AggressivePacingConfig)
    budget: AggressiveBudgetConfig = Field(default_factory=AggressiveBudgetConfig)


class AppConfig(BaseModel):
    run: RunConfig = Field(default_factory=RunConfig)
    pacing: PacingConfig = Field(default_factory=PacingConfig)
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    target_api: TargetApiConfig = Field(default_factory=TargetApiConfig)
    apify: ApifyConfig = Field(default_factory=ApifyConfig)
    aggressive: AggressiveConfig = Field(default_factory=AggressiveConfig)


def load_config(
    config_path: str | Path = "config.yaml",
    env_path: str | Path | None = ".env",
) -> AppConfig:
    """Load .env (if present) and config.yaml with ${ENV} expansion."""
    if env_path is not None:
        env_file = Path(env_path)
        if env_file.exists():
            load_dotenv(env_file, override=False)

    raw_text = Path(config_path).read_text(encoding="utf-8")
    raw_data = yaml.safe_load(raw_text) or {}
    resolved = _resolve_env(raw_data)
    return AppConfig.model_validate(resolved)
