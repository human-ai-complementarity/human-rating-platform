"""Placeholder substitution for rater terms.

Statement files stay generic; the facts that differ per study are filled in
here at render time. The set of placeholders is closed so a typo in a
statement file fails at read time (see ``source.read_statement``) rather than
reaching a rater as literal braces.
"""

from __future__ import annotations

import re

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

KNOWN_PLACEHOLDERS = frozenset({"study_name", "session_length", "content_warning_details"})


def find_placeholders(text: str) -> set[str]:
    return {match.group(1) for match in PLACEHOLDER_RE.finditer(text)}


def format_session_length(minutes: int) -> str:
    """ "45 minutes" / "1 hour" / "2 hours 30 minutes"."""

    def plural(count: int, noun: str) -> str:
        return f"{count} {noun}{'' if count == 1 else 's'}"

    if minutes < 60:
        return plural(minutes, "minute")
    hours, rest = divmod(minutes, 60)
    hour_part = plural(hours, "hour")
    return hour_part if rest == 0 else f"{hour_part} {plural(rest, 'minute')}"


def render_markdown(
    text: str,
    *,
    study_name: str,
    session_length: str,
    content_warning_details: str | None,
) -> str:
    values = {
        "study_name": study_name,
        "session_length": session_length,
        "content_warning_details": (content_warning_details or "").strip(),
    }

    def substitute(match: re.Match[str]) -> str:
        return values.get(match.group(1), match.group(0))

    return PLACEHOLDER_RE.sub(substitute, text)
