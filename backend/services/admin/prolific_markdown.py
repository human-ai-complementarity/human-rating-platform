"""Convert lightweight markdown to the HTML subset Prolific accepts.

Prolific's study description field renders a small, fixed set of HTML tags:
``<b> <strong> <i> <em> <s> <u> <h1> <h2> <ol> <ul> <li> <p>``. Anything else
is stripped silently — including bare newlines, which is why a textarea with
plain newlines collapses into a single paragraph on Prolific.

We accept markdown-ish input from researchers and emit only those tags. The
converter is intentionally minimal:

* Block-level: ``# H1``, ``## H2``, ``- list``, ``* list``, ``1. list``,
  blank-line-separated paragraphs, single newlines become ``<br>``.
* Inline: ``**bold**`` / ``__bold__`` → ``<b>``; ``*italic*`` / ``_italic_``
  → ``<i>``; ``~~strike~~`` → ``<s>``.
* Anything else (links, images, code blocks, tables, ``###`` and deeper
  headings) is passed through as escaped text so it shows up literally
  rather than getting silently dropped by Prolific.
"""

from __future__ import annotations

import re
from html import escape

# Bold patterns forbid the same delimiter character inside the match so that
# `**bold and *italic***` doesn't produce overlapping <b>…<i></b>…</i> — that
# is malformed and Prolific's sanitiser drops the whole subtree. With these
# patterns the input falls through as escaped literal so the researcher sees
# their malformed markdown and corrects it.
_BOLD_DOUBLE_STAR = re.compile(r"\*\*([^*]+?)\*\*", re.DOTALL)
_BOLD_DOUBLE_UNDERSCORE = re.compile(r"__([^_]+?)__", re.DOTALL)
_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_ITALIC_STAR = re.compile(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)")
_ITALIC_UNDERSCORE = re.compile(r"(?<![_\w])_(?!\s)([^_\n]+?)(?<!\s)_(?![_\w])")

_BULLET_PREFIX = re.compile(r"^\s*[-*]\s+")
_ORDERED_PREFIX = re.compile(r"^\s*\d+\.\s+")
_BLANK_LINE_SPLIT = re.compile(r"\n\s*\n")


def to_prolific_html(markdown: str) -> str:
    """Convert markdown-ish text to the HTML subset Prolific accepts.

    Empty / whitespace-only input returns an empty string so the caller can
    decide whether to forward it or substitute a default.
    """
    if not markdown or not markdown.strip():
        return ""

    text = markdown.replace("\r\n", "\n").replace("\r", "\n")
    blocks = _BLANK_LINE_SPLIT.split(text)

    rendered: list[str] = []
    for block in blocks:
        block = block.strip("\n")
        if not block.strip():
            continue
        rendered.append(_render_block(block))
    return "".join(rendered)


def _render_block(block: str) -> str:
    lines = block.split("\n")

    non_empty = [line for line in lines if line.strip()]
    if non_empty and all(_BULLET_PREFIX.match(line) for line in non_empty):
        items = [_BULLET_PREFIX.sub("", line, count=1).strip() for line in non_empty]
        return "<ul>" + "".join(f"<li>{_inline(item)}</li>" for item in items) + "</ul>"
    if non_empty and all(_ORDERED_PREFIX.match(line) for line in non_empty):
        items = [_ORDERED_PREFIX.sub("", line, count=1).strip() for line in non_empty]
        return "<ol>" + "".join(f"<li>{_inline(item)}</li>" for item in items) + "</ol>"

    # Walk the block line by line so adjacent headings (no blank line between
    # them) each render as their own tag, and non-heading runs accumulate into
    # a single <p>…<br>…</p>.
    parts: list[str] = []
    paragraph_lines: list[str] = []

    def flush_paragraph() -> None:
        if paragraph_lines:
            parts.append(_render_paragraph(paragraph_lines))
            paragraph_lines.clear()

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("## "):
            flush_paragraph()
            parts.append(f"<h2>{_inline(stripped[3:].strip())}</h2>")
        elif stripped.startswith("# "):
            flush_paragraph()
            parts.append(f"<h1>{_inline(stripped[2:].strip())}</h1>")
        else:
            paragraph_lines.append(line)
    flush_paragraph()
    return "".join(parts)


def _render_paragraph(lines: list[str]) -> str:
    inner = "<br>".join(_inline(line.strip()) for line in lines if line.strip())
    if not inner:
        return ""
    return f"<p>{inner}</p>"


def _inline(text: str) -> str:
    out = escape(text, quote=False)
    out = _BOLD_DOUBLE_STAR.sub(r"<b>\1</b>", out)
    out = _BOLD_DOUBLE_UNDERSCORE.sub(r"<b>\1</b>", out)
    out = _STRIKE.sub(r"<s>\1</s>", out)
    out = _ITALIC_STAR.sub(r"<i>\1</i>", out)
    out = _ITALIC_UNDERSCORE.sub(r"<i>\1</i>", out)
    return out
