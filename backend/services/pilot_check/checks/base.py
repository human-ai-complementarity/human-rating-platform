"""What a check is given and what it hands back."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from models import Experiment

from ..config import PilotCheckConfig, Severity

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from services.terms import RaterTerms

    from ..assistance import AssistanceProbe
    from ..judge import Judge
    from ..rater_view import RaterQuestionView


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
    config: PilotCheckConfig
    question_count: int
    views: list[RaterQuestionView]
    terms: RaterTerms | None = None
    terms_error: str | None = None
    probes: list[AssistanceProbe] | None = None
    judge: Judge | None = None
    judge_unavailable_reason: str | None = None
    assistance_instructions: str | None = None
    method_params: dict = field(default_factory=dict)
    # Set by the run while a judged check runs: how far along it is, for the
    # card's stepper ("9 of 15 questions reviewed"). Cosmetic, never fatal.
    progress: Callable[[str], Awaitable[None]] | None = None

    async def note(self, text: str) -> None:
        if self.progress is None:
            return
        try:
            await self.progress(text)
        except Exception:
            logger.exception("pilot check progress note failed")

    def probe_for(self, question_id: int) -> AssistanceProbe | None:
        for probe in self.probes or []:
            if probe.question_id == question_id:
                return probe
        return None


class Check(ABC):
    """One named check. `name` keys the config file and the registry.

    `run` returns findings at `severity`, the level the config gives this
    check; a check may emit `info` notes below it but never above.
    """

    name: ClassVar[str]
    # Judged checks need a judge; without one they report a skip.
    judged: ClassVar[bool] = False
    # Checks that only have something to do when an assistance method ran.
    needs_assistance: ClassVar[bool] = False

    @abstractmethod
    async def run(self, ctx: CheckContext, severity: Severity) -> list[Finding]: ...

    def finding(
        self,
        severity: Severity,
        message: str,
        *,
        question_id: int | None = None,
        detail: str | None = None,
    ) -> Finding:
        return Finding(
            check=self.name,
            severity=severity,
            message=message,
            question_id=question_id,
            detail=detail,
        )

    def skipped(self, reason: str) -> Finding:
        return self.finding("info", f"Skipped: {reason}")
