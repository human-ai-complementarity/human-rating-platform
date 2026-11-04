"""Shared plumbing for the checks that ask the judge."""

from __future__ import annotations

import logging

from ..config import Severity
from ..judge import JudgeError, Verdict
from .base import Check, CheckContext, Finding

logger = logging.getLogger(__name__)


class JudgedCheck(Check):
    judged = True

    def verdict_finding(
        self, verdict: Verdict, severity: Severity, *, question_id: int | None = None
    ) -> Finding | None:
        if verdict.ok:
            return None
        detail = "\n".join(f"- {issue}" for issue in verdict.issues) or None
        return self.finding(
            severity,
            verdict.summary or "The judge flagged this; see the issues.",
            question_id=question_id,
            detail=detail,
        )

    def judge_failed(self, exc: JudgeError) -> Finding:
        logger.warning("pilot check judge failed", extra={"attributes": {"check": self.name}})
        return self.finding("info", "Skipped: the judge call failed.", detail=str(exc))

    def no_judge(self, ctx: CheckContext) -> Finding:
        return self.skipped(ctx.judge_unavailable_reason or "no judge is configured")
