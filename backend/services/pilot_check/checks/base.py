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

from ..judge import JudgeError

if TYPE_CHECKING:
    from services.terms import RaterTerms

    from ..assistance import AssistanceProbe
    from ..judge import Judge, Verdict
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


def judge_failed(check: str, exc: JudgeError, *, question_ids: list[int] | None = None) -> Finding:
    logger.warning("pilot check judge failed", extra={"attributes": {"check": check}})
    # A warning, not info: a check whose judge never answered must not read
    # as passed. A judge skipped for want of a key is `no_judge`, info.
    if question_ids:
        message = "The judge call failed on this question."
    else:
        message = "The judge call failed, so this check did not run."
    return Finding(check, "warning", message, question_ids=question_ids or [], detail=str(exc))


# A dead judge fails every call the same way; stop after this many in a row
# rather than wait out the whole sample.
_MAX_JUDGE_FAILURES_IN_A_ROW = 3


async def judge_each(
    check: str, ctx: CheckContext, task: str, items: list[tuple[int, str]]
) -> tuple[list[Finding], list[tuple[int, Verdict]]]:
    """Ask the judge about each `(question_id, material)`.

    Returns the failures, one finding per question whose call failed (the
    same error on several questions merges into one row), and the verdicts
    that flagged something. One failure does not stop the others; three in
    a row do, with a finding saying how many questions went unreviewed.
    """
    assert ctx.judge is not None
    failed: list[Finding] = []
    flagged: list[tuple[int, Verdict]] = []
    in_a_row = 0
    total = len(items)
    await ctx.note(f"0 of {total} questions reviewed")
    for index, (question_id, material) in enumerate(items, start=1):
        try:
            verdict = await ctx.judge(task=task, material=material)
        except JudgeError as exc:
            failed.append(judge_failed(check, exc, question_ids=[question_id]))
            in_a_row += 1
            if in_a_row >= _MAX_JUDGE_FAILURES_IN_A_ROW and index < total:
                failed.append(
                    Finding(
                        check,
                        "warning",
                        f"The judge failed {in_a_row} times in a row, so the remaining "
                        f"{total - index} questions were not reviewed.",
                    )
                )
                break
            continue
        in_a_row = 0
        await ctx.note(f"{index} of {total} questions reviewed")
        if not verdict.ok:
            flagged.append((question_id, verdict))
    return failed, flagged


def verdict_finding(
    check: str, verdict: Verdict, severity: Severity, *, question_ids: list[int] | None = None
) -> Finding | None:
    if verdict.ok:
        return None
    detail = "\n".join(f"- {issue}" for issue in verdict.issues) or None
    return Finding(
        check,
        severity,
        verdict.summary or "The judge flagged this; see the issues.",
        question_ids=list(question_ids or []),
        detail=detail,
    )


_GROUP_TASK = """\
Below are the problems you flagged on each of several questions, one block \
per question. The same problem usually shows up on many questions in \
different words. Merge them: list each distinct problem once, with every \
question id it applies to and one or two sentences a researcher can act on. \
A problem specific to one question stays its own entry. Do not invent \
problems that are not in the blocks, and do not drop a question."""


async def grouped_findings(
    check: str, ctx: CheckContext, severity: Severity, flagged: list[tuple[int, Verdict]]
) -> list[Finding]:
    """The findings for a per-question judged check: one per distinct problem,
    naming every question it applies to.

    The per-question verdicts describe the same problem in different words,
    so with two or more flagged questions the judge is asked once more to
    merge them. If that call fails, or leaves a question out, the question
    keeps its own finding: nothing the judge flagged is dropped.
    """
    single = [
        verdict_finding(check, verdict, severity, question_ids=[qid]) for qid, verdict in flagged
    ]
    singles = [finding for finding in single if finding is not None]
    if len(singles) < 2 or ctx.judge is None:
        return singles
    await ctx.note(f"merging the problems on {len(singles)} questions")
    blocks = [
        f"[Question {finding.question_ids[0]}]\n{finding.detail or '- ' + finding.message}"
        for finding in singles
    ]
    try:
        problems = await ctx.judge.group(task=_GROUP_TASK, material="\n\n".join(blocks))
    except JudgeError:
        logger.warning("pilot check grouping failed", extra={"attributes": {"check": check}})
        return singles
    known = {finding.question_ids[0] for finding in singles}
    findings: list[Finding] = []
    covered: set[int] = set()
    for problem in problems:
        ids = [qid for qid in problem.question_ids if qid in known]
        if not ids or not problem.summary:
            continue
        covered.update(ids)
        detail = "\n".join(f"- {issue}" for issue in problem.issues) or None
        findings.append(Finding(check, severity, problem.summary, question_ids=ids, detail=detail))
    findings.extend(finding for finding in singles if finding.question_ids[0] not in covered)
    return findings
