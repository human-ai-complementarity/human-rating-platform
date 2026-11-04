"""Can the rater's card show this question at all."""

from __future__ import annotations

import re

from ..config import Severity
from .base import Check, CheckContext, Finding

# Mirrors the rater card's parseOptions (QuestionCard.tsx) and top_n's
# _parse_options: a '|' list, else labelled "A." / "1)" items, else lines,
# else commas.
_OPTION_LABEL = re.compile(r"(?:^|\s)(?:[A-Za-z]|\d{1,2})[.):]\s")


def parse_options(raw: str | None) -> list[str]:
    if not raw:
        return []
    if "|" in raw:
        return [part.strip() for part in raw.split("|") if part.strip()]
    starts = [match.start() for match in _OPTION_LABEL.finditer(raw)]
    if len(starts) > 1:
        chunks = [
            raw[start : starts[index + 1] if index + 1 < len(starts) else None].strip(" ,\r\n")
            for index, start in enumerate(starts)
        ]
        return [chunk for chunk in chunks if chunk]
    lines = [line.strip() for line in re.split(r"\r?\n+", raw) if line.strip()]
    if len(lines) > 1:
        return lines
    return [part.strip() for part in raw.split(",") if part.strip()]


class QuestionRenders(Check):
    name = "question_renders"

    async def run(self, ctx: CheckContext, severity: Severity) -> list[Finding]:
        softer: Severity = "warning" if severity == "error" else severity
        findings: list[Finding] = []
        cap = ctx.config.rendering.max_question_chars
        for view in ctx.views:
            response = view.response
            qid = view.question_id
            text = response.question_text or ""
            if not text.strip():
                findings.append(
                    self.finding(severity, "The question text is empty.", question_id=qid)
                )
            options = parse_options(response.options)
            if response.question_type == "MC":
                if not options:
                    findings.append(
                        self.finding(
                            severity,
                            "A multiple-choice question has no options, so the rater would "
                            "get a free-text box instead of choices.",
                            question_id=qid,
                        )
                    )
                elif len(options) < 2:
                    findings.append(
                        self.finding(
                            softer,
                            "A multiple-choice question has only one option.",
                            question_id=qid,
                            detail=response.options,
                        )
                    )
            if len(options) != len({option.casefold() for option in options}):
                findings.append(
                    self.finding(
                        softer,
                        "The options contain duplicates.",
                        question_id=qid,
                        detail=response.options,
                    )
                )
            if (
                view.question.parent_question_id is not None
                and not (response.parent_question_text or "").strip()
            ):
                findings.append(
                    self.finding(
                        severity,
                        "This is a sub-question but its parent's context text is empty.",
                        question_id=qid,
                    )
                )
            if len(text) > cap:
                findings.append(
                    self.finding(
                        softer,
                        f"The question text is {len(text):,} characters, over the "
                        f"{cap:,} the card shows without heavy scrolling.",
                        question_id=qid,
                    )
                )
        return findings
