"""Could a rater answer this question as it is displayed."""

from __future__ import annotations

from .base import CheckContext, CheckSpec, Finding, Severity, grouped_findings, judge_each, no_judge

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
    items = [(view.question_id, view.as_text()) for view in ctx.views]
    failed, flagged = await judge_each(NAME, ctx, _TASK, items)
    return failed + await grouped_findings(NAME, ctx, severity, flagged)


CHECK = CheckSpec(NAME, "Question coherence", run, "warning", judged=True)
