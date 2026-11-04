"""The pilot check's own configuration file (docs/pilot-check.md).

`backend/pilot_check.toml` lists every check with `enabled` and `severity`,
plus the sample size, the judge model, assistance thresholds and the launch
gate mode. It is read once per process; its SHA-256 is stored on every run so
an edit marks earlier runs stale.

Validation is strict on purpose: an unknown key or check name, or a check
missing from the file, fails at load rather than silently running with a
default. `Settings.pilot_check.config_path` says where the file is.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Literal

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config import BASE_DIR, get_settings

Severity = Literal["error", "warning", "info"]
GateMode = Literal["warn", "block", "off"]

SEVERITY_RANK: dict[str, int] = {"info": 0, "warning": 1, "error": 2}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SampleConfig(_Strict):
    size: int = Field(default=15, ge=1, le=50)
    seed: int | None = None


class JudgeConfig(_Strict):
    model: str = "openrouter/anthropic/claude-sonnet-4-6"
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


class AssistanceConfig(_Strict):
    per_question_timeout_seconds: int = Field(default=120, ge=5, le=600)
    max_failure_share: float = Field(default=0.0, ge=0.0, le=1.0)
    max_median_latency_seconds: float = Field(default=20.0, ge=0.0)


class RenderingConfig(_Strict):
    max_question_chars: int = Field(default=20000, ge=100)


class GateConfig(_Strict):
    mode: GateMode = "warn"
    severity: Severity = "warning"


class CheckConfig(_Strict):
    enabled: bool = True
    severity: Severity = "warning"


class PilotCheckConfig(_Strict):
    sample: SampleConfig = Field(default_factory=SampleConfig)
    judge: JudgeConfig = Field(default_factory=JudgeConfig)
    assistance: AssistanceConfig = Field(default_factory=AssistanceConfig)
    rendering: RenderingConfig = Field(default_factory=RenderingConfig)
    gate: GateConfig = Field(default_factory=GateConfig)
    checks: dict[str, CheckConfig] = Field(default_factory=dict)
    # SHA-256 of the file as read; "" for a config built in code.
    sha256: str = ""

    def severity_of(self, check_name: str) -> Severity:
        return self.checks[check_name].severity

    def enabled_checks(self) -> list[str]:
        return [name for name, check in self.checks.items() if check.enabled]


class PilotCheckConfigError(RuntimeError):
    """The config file is missing, unreadable or does not match the registry."""


def _resolve_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def parse_config(text: str, *, known_checks: tuple[str, ...]) -> PilotCheckConfig:
    """Validate the file's text against the registered check names."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PilotCheckConfigError(f"pilot check config is not valid TOML: {exc}") from exc
    try:
        config = PilotCheckConfig.model_validate(data)
    except ValidationError as exc:
        raise PilotCheckConfigError(f"pilot check config is invalid: {exc}") from exc

    listed = set(config.checks)
    known = set(known_checks)
    unknown = sorted(listed - known)
    missing = sorted(known - listed)
    if unknown:
        raise PilotCheckConfigError(
            f"pilot check config lists checks that do not exist: {', '.join(unknown)}"
        )
    if missing:
        raise PilotCheckConfigError(
            f"pilot check config must list every check; missing: {', '.join(missing)}"
        )
    config.sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return config


def load_config(path: Path, *, known_checks: tuple[str, ...]) -> PilotCheckConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PilotCheckConfigError(f"pilot check config not readable at {path}: {exc}") from exc
    return parse_config(text, known_checks=known_checks)


# Tests swap the process config for one with a different gate mode or sample
# size without touching the file; None means "the file".
_override: PilotCheckConfig | None = None


def set_config_override(config: PilotCheckConfig | None) -> None:
    global _override
    _override = config


@lru_cache(maxsize=1)
def _load_from_settings() -> PilotCheckConfig:
    from .checks.registry import check_names

    return load_config(
        _resolve_path(get_settings().pilot_check.config_path), known_checks=check_names()
    )


def get_pilot_check_config() -> PilotCheckConfig:
    """The process's config, read once from the file (or the test override)."""
    return _override if _override is not None else _load_from_settings()
