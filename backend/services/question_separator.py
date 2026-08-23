"""Legacy `--- QUESTION ---` delimiter: detect it, split it, migrate it.

Long-context questions used to concatenate the document and the question with
this delimiter inside `question_text`. The parent-row shape (`parent_question_id`)
stores the document once instead. This module is the single backend copy of the
old splitter, used to reject new uploads that still emit it and to rewrite any
rows that already landed.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

# Same pattern QuestionCard used to split on. Keep the regex identical so the
# migration rewrites exactly the rows the old frontend would have split.
_SEPARATOR_RE = re.compile(r"\r?\n\r?\n--- QUESTION ---\r?\n")

# Parent rows created by the migration. Prefixed so they don't collide with
# researcher-assigned question_ids from the pipeline.
_PARENT_ID_PREFIX = "__parent_"


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


def question_ids_with_separator(rows: list[dict]) -> list[str]:
    """Return `question_id` strings whose `question_text` uses the delimiter."""
    return [
        str(row.get("question_id") or "")
        for row in rows
        if split_separator_question(row.get("question_text") or "")
    ]


def _parent_question_id_string(document: str, taken: set[str]) -> str:
    digest = hashlib.sha256(document.encode("utf-8")).hexdigest()[:12]
    candidate = f"{_PARENT_ID_PREFIX}{digest}"
    if candidate not in taken:
        return candidate
    n = 2
    while f"{candidate}_{n}" in taken:
        n += 1
    return f"{candidate}_{n}"


def _taken_question_ids(connection: Connection, experiment_id: int) -> set[str]:
    return {
        question_id
        for (question_id,) in connection.execute(
            text("SELECT question_id FROM questions WHERE experiment_id = :experiment_id"),
            {"experiment_id": experiment_id},
        )
    }


def migrate_separator_questions(connection: Connection) -> tuple[int, int]:
    """Rewrite delimiter-shaped questions into parent rows.

    Skips rows that already have a parent and rows that are themselves a
    parent (their `question_text` is the document, and a genuine document
    might contain the delimiter as content). Children that share a document
    inside one experiment share one new parent.

    Returns `(parent_rows_created, children_rewritten)`. Idempotent: a second
    run finds nothing left to split.
    """
    rows = (
        connection.execute(
            text(
                """
                SELECT q.id, q.experiment_id, q.question_id, q.question_text
                FROM questions q
                WHERE q.parent_question_id IS NULL
                  AND q.question_text LIKE '%--- QUESTION ---%'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM questions child
                    WHERE child.parent_question_id = q.id
                  )
                """
            )
        )
        .mappings()
        .all()
    )

    grouped: dict[tuple[int, str], list[tuple[int, str]]] = defaultdict(list)
    for row in rows:
        split = split_separator_question(row["question_text"])
        if split is None:
            continue
        document, question_part = split
        grouped[(row["experiment_id"], document)].append((row["id"], question_part))

    if not grouped:
        return 0, 0

    taken_by_experiment: dict[int, set[str]] = {}
    parents_created = 0
    children_rewritten = 0
    for (experiment_id, document), children in grouped.items():
        if experiment_id not in taken_by_experiment:
            taken_by_experiment[experiment_id] = _taken_question_ids(connection, experiment_id)
        taken = taken_by_experiment[experiment_id]
        parent_qid = _parent_question_id_string(document, taken)
        taken.add(parent_qid)
        parent_db_id = connection.execute(
            text(
                """
                INSERT INTO questions (
                    experiment_id,
                    question_id,
                    question_text,
                    gt_answer,
                    options,
                    question_type,
                    extra_data
                )
                VALUES (
                    :experiment_id,
                    :question_id,
                    :question_text,
                    '',
                    '',
                    'MC',
                    '{}'
                )
                RETURNING id
                """
            ),
            {
                "experiment_id": experiment_id,
                "question_id": parent_qid,
                "question_text": document,
            },
        ).scalar_one()
        parents_created += 1
        for child_id, question_part in children:
            connection.execute(
                text(
                    """
                    UPDATE questions
                    SET question_text = :question_text,
                        parent_question_id = :parent_question_id
                    WHERE id = :id
                    """
                ),
                {
                    "question_text": question_part,
                    "parent_question_id": parent_db_id,
                    "id": child_id,
                },
            )
            children_rewritten += 1

    logger.info(
        "Migrated separator-shaped questions",
        extra={
            "attributes": {
                "parents_created": parents_created,
                "children_rewritten": children_rewritten,
            }
        },
    )
    return parents_created, children_rewritten
