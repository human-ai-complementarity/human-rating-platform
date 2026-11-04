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
    from ..judge import Judge, JudgeError, Verdict
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
    """Everything a run gathered before the checks look at it.

    `probes` is None on a control arm: no method ran, and the assistance
    checks say so rather than reporting "no assistance" as a failure.
    `judge` is None when the judge model's provider has no key, and judged
    checks report that as a skipped finding.
    """

    experiment: Experiment
    settings: PilotCheckSettings
    question_count: int
    views: list[RaterQuestionView]
    terms: RaterTerms | None = None
    terms_error: str | None = None
    probes: list[AssistanceProbe] | None = None
    judge: Judge | None = None
    judge_unavailable_reason: str | None = None
    assistance_instructions: str | None = None
    method_params: dict = field(default_factory=dict)
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

    def probe_for(self, question_id: int) -> AssistanceProbe | None:
        for probe in self.probes or []:
            if probe.question_id == question_id:
                return probe
        return None


CheckFn = Callable[[CheckContext, Severity], Awaitable[list[Finding]]]


@dataclass(frozen=True)
class CheckSpec:
    name: str
    # Shown on the card as the finding group's heading and the stage's name.
    label: str
    run: CheckFn
    # The level its findings get unless `settings.severities` overrides it.
    severity: Severity
    # Judged checks need a judge; without one they report a skip.
    judged: bool = False
    # Checks that only have something to do when an assistance method ran.
    needs_assistance: bool = False


def skipped(check: str, reason: str) -> Finding:
    return Finding(check, "info", f"Skipped: {reason}")


# --- Judged checks -----------------------------------------------------------


def no_judge(check: str, ctx: CheckContext) -> Finding:
    return skipped(check, ctx.judge_unavailable_reason or "no judge is configured")


def judge_failed(check: str, exc: JudgeError) -> Finding:
    logger.warning("pilot check judge failed", extra={"attributes": {"check": check}})
    # A warning, not info: a check whose judge never answered must not read
    # as passed. A judge skipped for want of a key is `no_judge`, info.
    return Finding(
        check, "warning", "The judge call failed, so this check did not run.", detail=str(exc)
    )


def verdict_finding(
    check: str, verdict: Verdict, severity: Severity, *, question_id: int | None = None
) -> Finding | None:
    if verdict.ok:
        return None
    detail = "\n".join(f"- {issue}" for issue in verdict.issues) or None
    return Finding(
        check,
        severity,
        verdict.summary or "The judge flagged this; see the issues.",
        question_id=question_id,
        detail=detail,
    )
