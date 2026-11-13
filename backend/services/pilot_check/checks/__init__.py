"""The checks a pilot run applies, in report order: experiment-level
configuration first, then what each sampled question looks like, then the
assistance on it, then the judge's reads, then the judge reading screenshots of
the rater page.

Adding a check: write a module with `NAME`, `run(ctx, severity)` and a
`CHECK = CheckSpec(...)`, and add it to `CHECKS`.
"""

from __future__ import annotations

from config import PilotCheckSettings

from . import (
    assistance_health,
    assistance_output,
    assistance_useful,
    consent,
    instructions,
    markdown_mode,
    question_coherent,
    question_renders,
    rater_screens,
)
from .base import SEVERITY_RANK, CheckContext, CheckSpec, Finding, Severity

CHECKS: tuple[CheckSpec, ...] = (
    consent.CHECK,
    markdown_mode.CHECK,
    question_renders.CHECK,
    assistance_output.CHECK,
    assistance_health.CHECK,
    instructions.CHECK,
    question_coherent.CHECK,
    assistance_useful.CHECK,
    rater_screens.CHECK,
)
CHECKS_BY_NAME: dict[str, CheckSpec] = {check.name: check for check in CHECKS}
assert len(CHECKS_BY_NAME) == len(CHECKS), "duplicate check name"


def severity_of(settings: PilotCheckSettings, check: CheckSpec) -> Severity:
    return settings.severities.get(check.name, check.severity)


def validate_severities(settings: PilotCheckSettings) -> None:
    """Refuse an override that names no check, at startup; a misspelt key
    would otherwise be ignored without a word. Lives here rather than on the
    settings model because config cannot import the checks."""
    unknown = sorted(set(settings.severities) - set(CHECKS_BY_NAME))
    if unknown:
        raise ValueError(
            "pilot_check.severities names no such check: "
            + ", ".join(unknown)
            + ". Known checks: "
            + ", ".join(CHECKS_BY_NAME)
        )


__all__ = [
    "CHECKS",
    "CHECKS_BY_NAME",
    "SEVERITY_RANK",
    "CheckContext",
    "CheckSpec",
    "Finding",
    "Severity",
    "severity_of",
    "validate_severities",
]
