"""What a check is given and what it hands back.

A check is one async function `run(ctx, severity) -> list[Finding]`, declared
to the run by a `CheckSpec` (see `checks/__init__.py`). `severity` is the
level the settings give the check; a check may emit `info` notes below it
but never above.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from config import PilotCheckSettings, PilotCheckSeverity
from models import Experiment

if TYPE_CHECKING:
    from services.terms import RaterTerms

    from ..rater_view import RaterQuestionView

logger = logging.getLogger(__name__)

Severity = PilotCheckSeverity
SEVERITY_RANK: dict[str, int] = {"info": 0, "warning": 1, "error": 2}


def soften(severity: Severity) -> Severity:
    """`error` becomes `warning`, for the findings a check is less sure of;
    anything else is unchanged."""
    return "warning" if severity == "error" else severity


@dataclass
class Finding:
    check: str
    severity: Severity
    message: str
    # Set on question-level findings so the report can open that question in
    # the participant preview.
    question_id: int | None = None
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "question_id": self.question_id,
            "detail": self.detail,
        }


@dataclass
class CheckContext:
    """Everything a run gathered before the checks look at it."""

    experiment: Experiment
    settings: PilotCheckSettings
    question_count: int
    views: list[RaterQuestionView]
    terms: RaterTerms | None = None
    terms_error: str | None = None
    # Set by the run while a check runs: how far along it is, for the card's
    # stepper ("9 of 15 questions reviewed"). Cosmetic, never fatal.
    report: Callable[[str], Awaitable[None]] | None = None

    async def note(self, text: str) -> None:
        if self.report is None:
            return
        try:
            await self.report(text)
        except Exception:
            logger.exception("pilot check progress note failed")


CheckFn = Callable[[CheckContext, Severity], Awaitable[list[Finding]]]


@dataclass(frozen=True)
class CheckSpec:
    name: str
    # Shown on the card as the finding group's heading and the stage's name.
    label: str
    run: CheckFn
    # The level its findings get unless `settings.severities` overrides it.
    severity: Severity


def skipped(check: str, reason: str) -> Finding:
    return Finding(check, "info", f"Skipped: {reason}")
