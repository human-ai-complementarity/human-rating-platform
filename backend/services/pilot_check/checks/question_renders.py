"""Can the rater's card show this question at all."""

from __future__ import annotations

from services.assistance.methods.top_n import parse_options

from .base import CheckContext, CheckSpec, Finding, Severity, soften

# Longer than this and the rater has to scroll inside the card.
_MAX_QUESTION_CHARS = 20000


NAME = "question_renders"


async def run(ctx: CheckContext, severity: Severity) -> list[Finding]:
    softer = soften(severity)
    findings: list[Finding] = []
    for view in ctx.views:
        response = view.response
        qid = view.question_id
        text = response.question_text or ""
        if not text.strip():
            findings.append(
                Finding(NAME, severity, "The question text is empty.", question_ids=[qid])
            )
        options = parse_options(response.options)
        if response.question_type == "MC":
            if not options:
                findings.append(
                    Finding(
                        NAME,
                        severity,
                        "A multiple-choice question has no options, so the rater would "
                        "get a free-text box instead of choices.",
                        question_ids=[qid],
                    )
                )
            elif len(options) < 2:
                findings.append(
                    Finding(
                        NAME,
                        softer,
                        "A multiple-choice question has only one option.",
                        question_ids=[qid],
                        detail=response.options,
                    )
                )
        if len(options) != len({option.casefold() for option in options}):
            findings.append(
                Finding(
                    NAME,
                    softer,
                    "The options contain duplicates.",
                    question_ids=[qid],
                    detail=response.options,
                )
            )
        if (
            view.question.parent_question_id is not None
            and not (response.parent_question_text or "").strip()
        ):
            findings.append(
                Finding(
                    NAME,
                    severity,
                    "This is a sub-question but its parent's context text is empty.",
                    question_ids=[qid],
                )
            )
        if len(text) > _MAX_QUESTION_CHARS:
            findings.append(
                Finding(
                    NAME,
                    softer,
                    f"The question text is {len(text):,} characters, over the "
                    f"{_MAX_QUESTION_CHARS:,} the card shows without heavy scrolling.",
                    question_ids=[qid],
                )
            )
    return findings


CHECK = CheckSpec(NAME, "Question rendering", run, "error")
