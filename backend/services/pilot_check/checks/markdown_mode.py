"""Does the Markdown switch match the text: structure that will show as
literal symbols when off, raw HTML or placeholders that will show when on.

The prompt prefix and suffix follow the same switch as the question text
(`PromptFraming` in `QuestionCard.tsx`), so both are held to the same rules.
"""

from __future__ import annotations

import re

from .base import CheckContext, CheckSpec, Finding, Severity


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


NAME = "markdown_mode"


async def run(ctx: CheckContext, severity: Severity) -> list[Finding]:
    exp = ctx.experiment
    findings: list[Finding] = []
    framing = {
        "prompt prefix": exp.human_prompt_prefix or "",
        "prompt suffix": exp.human_prompt_suffix or "",
    }
    if not exp.is_markdown:
        framed = [label for label, text in framing.items() if looks_like_markdown(text)]
        if framed:
            findings.append(
                Finding(
                    NAME,
                    severity,
                    f"Markdown rendering is off, but the {' and '.join(framed)} "
                    f"contain{'s' if len(framed) == 1 else ''} Markdown structure that "
                    "raters will see as literal symbols.",
                    detail="Turn on 'Render as Markdown' under Instructions & prompts, "
                    "or rewrite the text as plain prose.",
                )
            )
        marked = [
            view.question_id
            for view in ctx.views
            if looks_like_markdown(view.response.question_text)
        ]
        if marked:
            findings.append(
                Finding(
                    NAME,
                    severity,
                    "Markdown rendering is off, but these questions contain Markdown "
                    "structure that raters will see as literal symbols.",
                    question_ids=marked,
                    detail="Turn on 'Render as Markdown' under Instructions & prompts, "
                    "or rewrite the text as plain prose.",
                )
            )
    for view in ctx.views:
        text = view.response.question_text
        if exp.is_markdown and _HTML_TAG.search(text):
            findings.append(
                Finding(
                    NAME,
                    severity,
                    "Markdown rendering is on and the question text contains raw HTML "
                    "tags, which will be escaped and shown literally.",
                    question_ids=[view.question_id],
                )
            )
        if _PLACEHOLDER.search(text):
            findings.append(
                Finding(
                    NAME,
                    severity,
                    "The question text contains an unfilled {{placeholder}}.",
                    question_ids=[view.question_id],
                    detail=_PLACEHOLDER.search(text).group(0),  # type: ignore[union-attr]
                )
            )
    for label, text in framing.items():
        if exp.is_markdown and _HTML_TAG.search(text):
            findings.append(
                Finding(
                    NAME,
                    severity,
                    f"Markdown rendering is on and the {label} contains raw HTML tags, "
                    "which will be escaped and shown literally.",
                )
            )
        if _PLACEHOLDER.search(text):
            findings.append(
                Finding(NAME, severity, f"The {label} contains an unfilled {{{{placeholder}}}}.")
            )
    return findings


CHECK = CheckSpec(NAME, "Markdown mode", run, "warning")
