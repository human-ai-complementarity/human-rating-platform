"""Does the Markdown switch match the text: structure that will show as
literal symbols when off, raw HTML or placeholders that will show when on."""

from __future__ import annotations

import re

from ..config import Severity
from .base import Check, CheckContext, Finding

_MARKDOWN_STRUCTURE = re.compile(
    r"(^#{1,6}\s+\S)"  # heading
    r"|(^\s*[-*+]\s+\S)"  # bullet
    r"|(^\s*\d+\.\s+\S)"  # numbered item
    r"|(^```)"  # fence
    r"|(\[[^\]]+\]\([^)]+\))"  # link
    r"|(\*\*\S[^*]*\*\*)",  # bold
    re.MULTILINE,
)
_HTML_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9-]*(\s[^<>]*)?>")
_PLACEHOLDER = re.compile(r"\{\{\s*[A-Za-z0-9_]+\s*\}\}")


def looks_like_markdown(text: str) -> bool:
    return bool(_MARKDOWN_STRUCTURE.search(text))


class MarkdownMode(Check):
    name = "markdown_mode"

    async def run(self, ctx: CheckContext, severity: Severity) -> list[Finding]:
        exp = ctx.experiment
        findings: list[Finding] = []
        framing = {
            "prompt prefix": exp.human_prompt_prefix or "",
            "prompt suffix": exp.human_prompt_suffix or "",
        }
        if not exp.is_markdown:
            marked = [label for label, text in framing.items() if looks_like_markdown(text)]
            marked += [
                f"question {view.question_id}"
                for view in ctx.views
                if looks_like_markdown(view.response.question_text)
            ]
            if marked:
                findings.append(
                    self.finding(
                        severity,
                        "Markdown rendering is off, but these contain Markdown structure that "
                        "raters will see as literal symbols: " + ", ".join(marked[:8]),
                        detail="Turn on 'Render as Markdown' under Instructions & prompts, "
                        "or rewrite the text as plain prose.",
                    )
                )
        for view in ctx.views:
            text = view.response.question_text
            if exp.is_markdown and _HTML_TAG.search(text):
                findings.append(
                    self.finding(
                        severity,
                        "Markdown rendering is on and the question text contains raw HTML "
                        "tags, which will be escaped and shown literally.",
                        question_id=view.question_id,
                    )
                )
            if _PLACEHOLDER.search(text):
                findings.append(
                    self.finding(
                        severity,
                        "The question text contains an unfilled {{placeholder}}.",
                        question_id=view.question_id,
                        detail=_PLACEHOLDER.search(text).group(0),  # type: ignore[union-attr]
                    )
                )
        for label, text in framing.items():
            if _PLACEHOLDER.search(text):
                findings.append(
                    self.finding(severity, f"The {label} contains an unfilled {{{{placeholder}}}}.")
                )
        return findings
