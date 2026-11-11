"""Did the assistance method give this question something usable.

The degenerate shapes a researcher would spot in the participant preview,
and nothing subtler: no assistance at all, a top-N with no candidates, a
subtask list with nothing in it or nothing filled in, a synthesis with no
answer. Judging whether what came back is any good is the judge's job.
"""

from __future__ import annotations

from ..assistance import AssistanceProbe
from .base import CheckContext, CheckSpec, Finding, Severity, skipped

NAME = "assistance_output"


async def run(ctx: CheckContext, severity: Severity) -> list[Finding]:
    if ctx.probes is None:
        return [skipped(NAME, "the experiment has no assistance method")]
    findings: list[Finding] = []
    for probe in ctx.probes:
        message, detail = _problem(probe, ctx.experiment.assistance_method)
        if message:
            findings.append(
                Finding(NAME, severity, message, question_ids=[probe.question_id], detail=detail)
            )
    return findings


def _problem(probe: AssistanceProbe, method: str) -> tuple[str | None, str | None]:
    if not probe.succeeded:
        reason = probe.failure_reason or "no reason recorded"
        return f"No assistance came back for this question ({reason}).", probe.failure_detail
    payload = probe.payload or {}
    if payload.get("kind") == "top_n" or method == "top_n":
        if not payload.get("candidates"):
            return "Top-N returned no candidate answers.", None
        return None, None
    if method == "human_as_a_tool":
        if probe.step_type == "complete":
            answer = str((payload.get("synthesis") or {}).get("answer") or "").strip()
            if not answer:
                return (
                    "The AI synthesised a final answer before asking the rater anything, "
                    "and the answer is empty.",
                    None,
                )
            return None, None
        subtasks = payload.get("subtasks") or []
        if not subtasks:
            return "The AI produced no subtasks for the rater.", None
        if all(not _answer_of(subtask) for subtask in subtasks):
            zeros = all(_confidence(subtask) == 0 for subtask in subtasks)
            return (
                f"None of the {len(subtasks)} subtasks has an AI answer filled in"
                + (" and every confidence is 0%" if zeros else "")
                + "; the rater would see an empty form.",
                None,
            )
    return None, None


def _confidence(subtask: dict) -> int:
    try:
        return int(subtask.get("confidence", 0))
    except (TypeError, ValueError):
        return 0


def _answer_of(subtask: dict) -> str:
    index = subtask.get("my_answer_index")
    if index is not None and index != "":
        return str(index)
    return str(subtask.get("my_answer") or "").strip()


CHECK = CheckSpec(NAME, "Assistance output", run, "error", needs_assistance=True)
