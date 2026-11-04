"""Is the assistance about this question, and in the shape raters were promised."""

from __future__ import annotations

import json

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
Below is one question as the rater sees it, what raters were told the AI \
assistance does, and the assistance payload the platform produced for this \
question (the JSON the rater's panel renders). Decide whether the assistance \
is about this question and would plausibly help. Flag: assistance about a \
different question or topic; candidate answers that are not among the \
question's options when it has options; subtasks that merely restate the \
question; answers or rationales that are empty, placeholder text, or \
nonsense; a shape that contradicts what raters were told. Do not judge \
whether the AI's answers are correct."""

_PAYLOAD_CHARS = 6000


NAME = "assistance_useful"


async def run(ctx: CheckContext, severity: Severity) -> list[Finding]:
    if ctx.probes is None:
        return [skipped(NAME, "the experiment has no assistance method")]
    if ctx.judge is None:
        return [no_judge(NAME, ctx)]
    findings: list[Finding] = []
    # Only questions whose probe landed; assistance_output reports the rest.
    targets = [
        (view, probe)
        for view in ctx.views
        if (probe := ctx.probe_for(view.question_id)) is not None and probe.succeeded
    ]
    await ctx.note(f"0 of {len(targets)} questions reviewed")
    for index, (view, probe) in enumerate(targets, start=1):
        payload = json.dumps(probe.payload, indent=1, ensure_ascii=False)
        if len(payload) > _PAYLOAD_CHARS:
            payload = payload[:_PAYLOAD_CHARS] + "\n… (truncated)"
        material = (
            f"{view.as_text()}\n\n"
            f"[What raters are told about the assistance]\n"
            f"{ctx.assistance_instructions or '(nothing)'}\n\n"
            f"[Assistance payload, step type {probe.step_type}]\n{payload}"
        )
        try:
            verdict = await ctx.judge(task=_TASK, material=material)
        except JudgeError as exc:
            findings.append(judge_failed(NAME, exc))
            break
        await ctx.note(f"{index} of {len(targets)} questions reviewed")
        finding = verdict_finding(NAME, verdict, severity, question_id=view.question_id)
        if finding:
            findings.append(finding)
    return findings


CHECK = CheckSpec(NAME, "Assistance usefulness", run, "warning", judged=True, needs_assistance=True)
