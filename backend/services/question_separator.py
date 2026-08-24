"""Detect the legacy `--- QUESTION ---` delimiter in uploaded question text.

Long-context questions used to concatenate the document and the question with
this delimiter inside `question_text`. New uploads must use a parent row
instead. The rewrite of rows that already landed lives in the Alembic revision
that introduced this change, not here — that file is the frozen snapshot.
"""

from __future__ import annotations

import re

# Same pattern QuestionCard used to split on, and the same pattern the
# 20260823000000 Alembic revision uses to rewrite existing rows.
_SEPARATOR_RE = re.compile(r"\r?\n\r?\n--- QUESTION ---\r?\n")


def split_separator_question(question_text: str) -> tuple[str, str] | None:
    """Split delimiter-shaped text into (document, question).

    Uses the last match, matching the old frontend splitter. Returns None when
    the delimiter is absent or either side is empty after trim.
    """
    matches = list(_SEPARATOR_RE.finditer(question_text))
    if not matches:
        return None
    separator = matches[-1]
    document = question_text[: separator.start()].strip()
    question = question_text[separator.end() :].strip()
    if not document or not question:
        return None
    return document, question


def separator_upload_offenders(rows: list[dict]) -> list[str]:
    """Labels for upload rows that still concatenate document + question.

    The last row whose `question_id` another row in the same file points at is
    skipped: that is the document last-write-wins will attach the child to, and
    a genuine document may contain the delimiter as content. Earlier rows that
    share the same `question_id` string are not parents — they would be inserted
    as standalone questions — so a concatenated duplicate still fails the guard.
    Rows with a blank `question_id` are labeled by 1-based position so they
    cannot slip through. Duplicate labels are collapsed so the error preview
    does not repeat an id.
    """
    referenced_parents = {str(row.get("parent_question_id") or "").strip() for row in rows}
    referenced_parents.discard("")

    last_index_by_id: dict[str, int] = {}
    for index, row in enumerate(rows):
        question_id = str(row.get("question_id") or "").strip()
        if question_id:
            last_index_by_id[question_id] = index

    labels: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        question_id = str(row.get("question_id") or "").strip()
        if (
            question_id
            and question_id in referenced_parents
            and last_index_by_id[question_id] == index
        ):
            continue
        if split_separator_question(str(row.get("question_text") or "")) is None:
            continue
        label = f"'{question_id}'" if question_id else f"row {index + 1}"
        if label in seen:
            continue
        seen.add(label)
        labels.append(label)
    return labels
