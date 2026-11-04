"""Did the assistance method give this question something usable.

Method-specific rules over the first step's payload. The degenerate shapes
here are the ones a researcher would spot in the participant preview: a
subtask list with every confidence at 0 and nothing filled in, a top-N
with no candidates, a method that returned no assistance at all.
"""

from __future__ import annotations

from ..assistance import AssistanceProbe
from .base import CheckContext, CheckSpec, Finding, Severity, skipped, soften


def _confidences(subtasks: list[dict]) -> list[int]:
    values = []
    for subtask in subtasks:
        try:
            values.append(int(subtask.get("confidence", 0)))
        except (TypeError, ValueError):
            values.append(0)
    return values


def _answer_of(subtask: dict) -> str:
    answer = subtask.get("my_answer")
    index = subtask.get("my_answer_index")
    if index is not None and index != "":
        return str(index)
    return str(answer or "").strip()


NAME = "assistance_output"


async def run(ctx: CheckContext, severity: Severity) -> list[Finding]:
    if ctx.probes is None:
        return [skipped(NAME, "the experiment has no assistance method")]
    softer = soften(severity)
    findings: list[Finding] = []
    for probe in ctx.probes:
        findings.extend(_for_probe(probe, severity, softer, ctx.experiment.assistance_method))
    return findings


def _for_probe(
    probe: AssistanceProbe, severity: Severity, softer: Severity, method: str
) -> list[Finding]:
    qid = probe.question_id
    if not probe.succeeded:
        reason = probe.failure_reason or "no reason recorded"
        return [
            Finding(
                NAME,
                severity,
                f"No assistance came back for this question ({reason}).",
                question_id=qid,
                detail=probe.failure_detail,
            )
        ]
    payload = probe.payload or {}
    if payload.get("kind") == "top_n" or method == "top_n":
        return _top_n(payload, qid, severity, softer)
    if method == "human_as_a_tool":
        return _human_as_a_tool(probe, qid, severity, softer)
    return []


def _top_n(payload: dict, qid: int, severity: Severity, softer: Severity) -> list[Finding]:
    candidates = payload.get("candidates") or []
    if not candidates:
        return [Finding(NAME, severity, "Top-N returned no candidate answers.", question_id=qid)]
    answers = {str(c.get("answer", "")).strip().casefold() for c in candidates}
    findings: list[Finding] = []
    if len(candidates) > 1 and len(answers) == 1:
        findings.append(
            Finding(NAME, softer, "Every top-N candidate is the same answer.", question_id=qid)
        )
    if all(not str(c.get("rationale", "")).strip() for c in candidates):
        findings.append(
            Finding(NAME, "info", "No top-N candidate carries a rationale.", question_id=qid)
        )
    confidences = [int(c.get("confidence", 50)) for c in candidates]
    if confidences and all(value == 0 for value in confidences):
        findings.append(
            Finding(NAME, softer, "Every top-N candidate reports 0% confidence.", question_id=qid)
        )
    return findings


def _human_as_a_tool(
    probe: AssistanceProbe, qid: int, severity: Severity, softer: Severity
) -> list[Finding]:
    payload = probe.payload or {}
    if probe.step_type == "complete":
        synthesis = payload.get("synthesis") or {}
        answer = str(synthesis.get("answer", "")).strip()
        if not answer:
            return [
                Finding(
                    NAME,
                    severity,
                    "The AI synthesised a final answer before asking the rater anything, "
                    "and the answer is empty.",
                    question_id=qid,
                )
            ]
        return [
            Finding(
                NAME,
                softer,
                "The AI synthesised a final answer before asking the rater anything; "
                "the rater gets no subtasks to review.",
                question_id=qid,
                detail=answer[:300],
            )
        ]
    subtasks = payload.get("subtasks") or []
    if not subtasks:
        return [
            Finding(NAME, severity, "The AI produced no subtasks for the rater.", question_id=qid)
        ]
    findings: list[Finding] = []
    confidences = _confidences(subtasks)
    answers = [_answer_of(subtask) for subtask in subtasks]
    if all(value == 0 for value in confidences):
        findings.append(
            Finding(
                NAME,
                severity,
                f"Every one of the {len(subtasks)} subtasks has 0% confidence.",
                question_id=qid,
            )
        )
    elif len(set(confidences)) == 1 and len(confidences) > 1:
        findings.append(
            Finding(
                NAME,
                softer,
                f"Every subtask has the same confidence ({confidences[0]}%); the "
                "estimator may not be discriminating.",
                question_id=qid,
            )
        )
    if all(not answer for answer in answers):
        findings.append(
            Finding(
                NAME,
                severity,
                "No subtask has an AI answer filled in; the rater would see an empty form.",
                question_id=qid,
            )
        )
    if all(not str(subtask.get("question", "")).strip() for subtask in subtasks):
        findings.append(
            Finding(NAME, severity, "The subtasks have no question text.", question_id=qid)
        )
    threshold = int(payload.get("confidence_threshold", 75))
    if confidences and all(value >= threshold for value in confidences):
        findings.append(
            Finding(
                NAME,
                "info",
                f"All subtasks are at or above the {threshold}% threshold, so nothing "
                "is delegated to the rater on this question.",
                question_id=qid,
            )
        )
    return findings


CHECK = CheckSpec(NAME, "Assistance output", run, "error", needs_assistance=True)
