"""Central backend configuration surface.

Design goals for contributors:
1. Keep config contract explicit and small.
2. Prefer nested keys for both TOML and env overrides.
3. Keep overrides ergonomic in real deployments (ignore unrelated env keys).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    InitSettingsSource,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

BASE_DIR = Path(__file__).resolve().parent


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AppSettings(_StrictModel):
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["*"],
    )
    site_url: str = "http://localhost:5173"
    log_level: str = "INFO"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> list[str]:
        err = (
            "APP__CORS_ORIGINS must be a JSON array of strings, "
            "for example '[\"https://app.example.com\"]'."
        )
        if value is None:
            return ["*"]

        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(err) from exc

        if not isinstance(value, list):
            raise ValueError(err)

        if any(not isinstance(item, str) for item in value):
            raise ValueError(err)

        return [item.strip() for item in value if item.strip()]


class DatabaseSettings(_StrictModel):
    url: str = "postgresql://postgres:postgres@localhost:5432/human_rating_platform"


class ExportSettings(_StrictModel):
    stream_batch_size: int = Field(
        default=1000,
        ge=1,
    )


class TestingSettings(_StrictModel):
    export_seed_row_count: int = Field(
        default=1500,
        ge=1,
    )


class ClerkSettings(_StrictModel):
    issuer: str = ""
    jwks_url: str = ""
    audience: str = "human-rating-platform-admin-api"


class SeedingSettings(_StrictModel):
    enabled: bool = False
    experiment_name: str = "Seed - Local Baseline"
    question_count: int = Field(default=50, ge=1)
    num_ratings_per_question: int = Field(default=3, ge=1)
    prolific_completion_url: str | None = None


class ProlificSettings(_StrictModel):
    api_token: str = ""
    base_url: str = "https://api.prolific.com/api/v1"
    project_id: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.api_token.strip())


class LLMModels:
    CLAUDE_SONNET = "openrouter/anthropic/claude-sonnet-4-6"
    CLAUDE_HAIKU = "openrouter/anthropic/claude-haiku-4-5"
    GPT_4O = "openrouter/openai/gpt-4o"
    GPT_4O_MINI = "openrouter/openai/gpt-4o-mini"
    GEMINI_3_FLASH_PREVIEW = "openrouter/google/gemini-3-flash-preview"
    GEMINI_FLASH = "openrouter/google/gemini-2.0-flash"
    GEMINI_FLASH_LITE = "openrouter/google/gemini-2.5-flash-lite"
    LLAMA_70B = "openrouter/meta-llama/llama-3.3-70b-instruct"


class LLMSettings(_StrictModel):
    default_model: str = LLMModels.CLAUDE_SONNET
    decomposition_model: str = LLMModels.GEMINI_3_FLASH_PREVIEW
    confidence_model: str = LLMModels.GEMINI_FLASH_LITE
    openrouter_api_key: str = ""
    max_tokens: int = 4096
    request_timeout: int = 60
    max_retries: int = 2


class Settings(BaseSettings):
    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    exports: ExportSettings = Field(default_factory=ExportSettings)
    testing: TestingSettings = Field(default_factory=TestingSettings)
    clerk: ClerkSettings = Field(default_factory=ClerkSettings)
    seeding: SeedingSettings = Field(default_factory=SeedingSettings)
    prolific: ProlificSettings = Field(default_factory=ProlificSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)

    # Admin/session config (mapped from flat env vars for ergonomics)
    admin_auth_enabled: bool = Field(default=True)
    admin_allowlist: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Comma-separated list of allowlisted admin emails.",
    )
    app_secret_key: str = Field(
        description="Secret for signing the HTTP-only admin session cookie.",
    )
    # Dedicated secret for rater session tokens. If not provided, falls back to app_secret_key
    rater_session_secret_key: str | None = Field(
        default=None,
        description="Secret for signing rater session tokens (fallbacks to app_secret_key if unset).",
    )
    # Rater-session token TTL in seconds. Defaults to the session duration (60 minutes).
    rater_session_ttl_seconds: int = Field(default=60 * 60)
    hrp_session_cookie: str = Field(default="hrp_session")
    hrp_session_max_age: int = Field(default=60 * 60 * 24 * 7)  # 7 days
    cookie_secure: bool = Field(default=False)

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        toml_file=BASE_DIR / "config.toml",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: InitSettingsSource,
        env_settings: EnvSettingsSource,
        dotenv_settings: DotEnvSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Highest priority first:
        # constructor kwargs > process env > .env > config.toml > file secrets.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    @field_validator("admin_allowlist", mode="before")
    @classmethod
    def parse_admin_allowlist(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            # Allow JSON array or comma-separated string
            value = value.strip()
            if value.startswith("["):
                try:
                    arr = json.loads(value)
                    if isinstance(arr, list):
                        return [str(x).strip() for x in arr if str(x).strip()]
                except json.JSONDecodeError:
                    pass
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(x).strip() for x in value if str(x).strip()]
        return []

    @property
    def sync_database_url(self) -> str:
        url = self.database.url.strip()
        if url.startswith("postgresql+asyncpg://"):
            return f"postgresql://{url.removeprefix('postgresql+asyncpg://')}"
        if url.startswith("postgresql://"):
            return url
        raise RuntimeError("DATABASE__URL must start with postgresql:// or postgresql+asyncpg://")

    @property
    def async_database_url(self) -> str:
        return self.sync_database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    @property
    def effective_rater_session_secret(self) -> str:
        # Prefer dedicated secret; fallback to app_secret_key for backward compatibility
        return (self.rater_session_secret_key or self.app_secret_key).strip()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
