"""Could a rater answer this question as it is displayed."""

from __future__ import annotations

from ..judge import JudgeError
from .base import (
    CheckContext,
    CheckSpec,
    Finding,
    Severity,
    judge_failed,
    no_judge,
    verdict_finding,
)

_TASK = """\
Below is one question exactly as the rater's card shows it, in display order, \
with the rendering mode stated. Decide whether a rater could answer it. Flag: \
text that is not a question or task; options that do not match the question, \
are missing for a choice question, or are a single run-on string; content cut \
off mid-sentence; leaked metadata, ids, JSON, or pipeline notes; a table, \
code block or list that will not render in the stated mode; references to \
material ("the passage above", "option C") that is not there. Do not flag \
difficulty, length alone, or the subject matter."""


NAME = "question_coherent"


async def run(ctx: CheckContext, severity: Severity) -> list[Finding]:
    if ctx.judge is None:
        return [no_judge(NAME, ctx)]
    findings: list[Finding] = []
    total = len(ctx.views)
    await ctx.note(f"0 of {total} questions reviewed")
    for index, view in enumerate(ctx.views, start=1):
        try:
            verdict = await ctx.judge(task=_TASK, material=view.as_text())
        except JudgeError as exc:
            findings.append(judge_failed(NAME, exc))
            break
        await ctx.note(f"{index} of {total} questions reviewed")
        finding = verdict_finding(NAME, verdict, severity, question_id=view.question_id)
        if finding:
            findings.append(finding)
    return findings


CHECK = CheckSpec(NAME, "Question coherence", run, "warning", judged=True)
