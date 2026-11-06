"""Do the rater instructions describe the task the questions actually pose."""

from __future__ import annotations

from ..judge import JudgeError
from .base import (
    CheckContext,
    CheckSpec,
    Finding,
    Severity,
    judge_failed,
    no_judge,
    skipped,
    verdict_finding,
)

_TASK = """\
Below are the instructions a rater reads once before starting, the framing \
text shown above and below every question, which assistance method (if any) \
is on and what raters are told about it, and a few of the questions. Decide \
whether a rater who read the instructions would know what to do on these \
questions. Flag: instructions that describe a different task than the \
questions pose; a scale, label set or answer format the instructions name \
that the questions do not offer (or the reverse); the framing contradicting \
the instructions; the instructions describing AI help that is off, or saying \
nothing about AI help that is on; leftover template text, placeholders or \
notes to self; anything a rater has to guess."""

_EXAMPLE_QUESTIONS = 3


NAME = "instructions_coherent"


async def run(ctx: CheckContext, severity: Severity) -> list[Finding]:
    if ctx.judge is None:
        return [no_judge(NAME, ctx)]
    exp = ctx.experiment
    if not (exp.description or "").strip():
        return [skipped(NAME, "there are no rater instructions to read")]
    parts = [f"[Rater instructions, shown once on the intro screen]\n{exp.description}"]
    if exp.human_prompt_prefix:
        parts.append(f"[Shown above every question]\n{exp.human_prompt_prefix}")
    if exp.human_prompt_suffix:
        parts.append(f"[Shown below every question]\n{exp.human_prompt_suffix}")
    if exp.assistance_method == "none":
        parts.append("[Assistance method]\nnone: raters get no AI help.")
    else:
        parts.append(
            f"[Assistance method]\n{exp.assistance_method}\n"
            f"[What raters are told about it]\n{ctx.assistance_instructions or '(nothing)'}"
        )
    examples = ctx.views[:_EXAMPLE_QUESTIONS]
    for index, view in enumerate(examples, start=1):
        parts.append(f"[Example question {index}]\n{view.as_text()}")
    await ctx.note(
        f"reading the instructions with {len(examples)} example question"
        f"{'' if len(examples) == 1 else 's'}"
    )
    try:
        verdict = await ctx.judge(task=_TASK, material="\n\n".join(parts))
    except JudgeError as exc:
        return [judge_failed(NAME, exc)]
    finding = verdict_finding(NAME, verdict, severity)
    return [finding] if finding else []


CHECK = CheckSpec(NAME, "Instructions", run, "warning", judged=True)
