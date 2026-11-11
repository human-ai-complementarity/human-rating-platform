"""What a check is given and what it hands back.

A check is one async function `run(ctx, severity) -> list[Finding]`, declared
to the run by a `CheckSpec` (see `checks/__init__.py`). `severity` is the
level the settings give the check; a check may emit `info` notes below it
but never above.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from config import PilotCheckSettings, PilotCheckSeverity
from models import Experiment

if TYPE_CHECKING:
    from services.terms import RaterTerms

    from ..assistance import AssistanceProbe
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
    # The questions this applies to, so the report can open each in the
    # participant preview. Empty for an experiment-level finding.
    question_ids: list[int] = field(default_factory=list)
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "question_ids": self.question_ids,
            "detail": self.detail,
        }


def merge_findings(findings: list[Finding]) -> list[Finding]:
    """One row per problem: findings that say the same thing (check, severity,
    message, detail) become one, naming every question they came from, in
    first-seen order."""
    merged: dict[tuple, Finding] = {}
    for finding in findings:
        key = (finding.check, finding.severity, finding.message, finding.detail)
        if key in merged:
            seen = merged[key].question_ids
            seen.extend(qid for qid in finding.question_ids if qid not in seen)
        else:
            merged[key] = Finding(
                finding.check,
                finding.severity,
                finding.message,
                question_ids=list(finding.question_ids),
                detail=finding.detail,
            )
    return list(merged.values())


@dataclass
class CheckContext:
    """Everything a run gathered before the checks look at it.

    `probes` is None on a control arm: no method ran, and the assistance
    checks say so rather than reporting "no assistance" as a failure.
    """

    experiment: Experiment
    settings: PilotCheckSettings
    question_count: int
    views: list[RaterQuestionView]
    terms: RaterTerms | None = None
    terms_error: str | None = None
    probes: list[AssistanceProbe] | None = None
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
    # Checks that only have something to do when an assistance method ran.
    needs_assistance: bool = False


def skipped(check: str, reason: str) -> Finding:
    return Finding(check, "info", f"Skipped: {reason}")
