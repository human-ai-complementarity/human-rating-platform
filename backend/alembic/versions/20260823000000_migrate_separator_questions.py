"""Rewrite --- QUESTION --- rows into parent_question_id shape.

Revision ID: 20260823000000
Revises: 20260819000000
Create Date: 2026-08-23 00:00:00.000000

Frozen snapshot of the delimiter rewrite. Do not import application services
from here: this file has to keep working after the live splitter is deleted.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text
from sqlalchemy.engine import Connection

revision: str = "20260823000000"
down_revision: Union[str, Sequence[str], None] = "20260819000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

# Identical to the regex QuestionCard used to split on. Frozen here so this
# revision does not depend on live application code.
_SEPARATOR_RE = re.compile(r"\r?\n\r?\n--- QUESTION ---\r?\n")
_PARENT_ID_PREFIX = "__parent_"

_CANDIDATE_CHILDREN_SQL = """
    SELECT q.id
    FROM questions q
    WHERE q.parent_question_id IS NULL
      AND q.question_text LIKE '%--- QUESTION ---%'
      AND NOT EXISTS (
        SELECT 1
        FROM questions child
        WHERE child.parent_question_id = q.id
      )
"""


def _split_separator_question(question_text: str) -> tuple[str, str] | None:
    matches = list(_SEPARATOR_RE.finditer(question_text))
    if not matches:
        return None
    separator = matches[-1]
    document = question_text[: separator.start()].strip()
    question = question_text[separator.end() :].strip()
    if not document or not question:
        return None
    return document, question


def _parent_question_id(digest: str, taken: set[str]) -> str:
    candidate = f"{_PARENT_ID_PREFIX}{digest[:12]}"
    if candidate not in taken:
        return candidate
    n = 2
    while f"{candidate}_{n}" in taken:
        n += 1
    return f"{candidate}_{n}"


def _warn_already_parented_delimiter_rows(connection: Connection) -> None:
    """Log delimiter-shaped children that already have a parent.

    Those rows used both mechanisms; after the frontend drops splitter parsing
    they render the concatenated blob on the card. Almost certainly zero, but
    a silent skip would hide the one case this revision makes worse.
    """
    candidate_ids = (
        connection.execute(
            text(
                """
            SELECT id
            FROM questions
            WHERE parent_question_id IS NOT NULL
              AND question_text LIKE '%--- QUESTION ---%'
            """
            )
        )
        .scalars()
        .all()
    )
    skipped = 0
    for question_pk in candidate_ids:
        question_text = connection.execute(
            text("SELECT question_text FROM questions WHERE id = :id"),
            {"id": question_pk},
        ).scalar_one()
        if _split_separator_question(question_text) is not None:
            skipped += 1
    if skipped:
        logger.warning(
            "Skipped %s delimiter-shaped question(s) that already have a parent; "
            "they will render concatenated on the rating card",
            skipped,
        )


def _migrate_experiment(connection: Connection, experiment_id: int) -> tuple[int, int]:
    # Ids first, then one row's text at a time. These are the documents that
    # OOM-killed the database; a streaming cursor held open across INSERTs on
    # the same connection is also unsafe, so we never materialize more than
    # one concatenated blob.
    child_ids = (
        connection.execute(
            text(_CANDIDATE_CHILDREN_SQL + " AND q.experiment_id = :experiment_id ORDER BY q.id"),
            {"experiment_id": experiment_id},
        )
        .scalars()
        .all()
    )
    if not child_ids:
        return 0, 0

    taken = {
        question_id
        for (question_id,) in connection.execute(
            text("SELECT question_id FROM questions WHERE experiment_id = :experiment_id"),
            {"experiment_id": experiment_id},
        )
    }
    parent_pk_by_digest: dict[str, int] = {}
    parents_created = 0
    children_rewritten = 0

    for child_id in child_ids:
        question_text = connection.execute(
            text("SELECT question_text FROM questions WHERE id = :id"),
            {"id": child_id},
        ).scalar_one()
        split = _split_separator_question(question_text)
        if split is None:
            continue
        document, question_part = split
        digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
        parent_pk = parent_pk_by_digest.get(digest)
        if parent_pk is None:
            parent_qid = _parent_question_id(digest, taken)
            taken.add(parent_qid)
            parent_pk = connection.execute(
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
            parent_pk_by_digest[digest] = parent_pk
            parents_created += 1
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
                "parent_question_id": parent_pk,
                "id": child_id,
            },
        )
        children_rewritten += 1

    return parents_created, children_rewritten


def migrate_separator_questions(connection: Connection) -> tuple[int, int]:
    """Rewrite delimiter-shaped questions into parent rows.

    Processes one experiment at a time and one concatenated blob at a time.
    Shared documents inside an experiment share one new parent. Returns
    `(parent_rows_created, children_rewritten)`. Idempotent.
    """
    _warn_already_parented_delimiter_rows(connection)

    experiment_ids = (
        connection.execute(
            text(
                """
            SELECT DISTINCT q.experiment_id
            FROM questions q
            WHERE q.parent_question_id IS NULL
              AND q.question_text LIKE '%--- QUESTION ---%'
              AND NOT EXISTS (
                SELECT 1
                FROM questions child
                WHERE child.parent_question_id = q.id
              )
            ORDER BY q.experiment_id
            """
            )
        )
        .scalars()
        .all()
    )

    parents_created = 0
    children_rewritten = 0
    for experiment_id in experiment_ids:
        created, rewritten = _migrate_experiment(connection, experiment_id)
        parents_created += created
        children_rewritten += rewritten

    logger.info(
        "Migrated separator-shaped questions: %s parent(s), %s child(ren)",
        parents_created,
        children_rewritten,
    )
    return parents_created, children_rewritten


def upgrade() -> None:
    migrate_separator_questions(op.get_bind())


def downgrade() -> None:
    # Cannot distinguish migrated rows from originally parent-shaped ones, so
    # this does not re-concatenate. Rolling back the frontend change is what
    # restores delimiter rendering if needed.
    pass
