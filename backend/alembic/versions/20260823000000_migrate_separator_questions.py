"""Rewrite --- QUESTION --- rows into parent_question_id shape.

Revision ID: 20260823000000
Revises: 20260819000000
Create Date: 2026-08-23 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

from services.question_separator import migrate_separator_questions

revision: str = "20260823000000"
down_revision: Union[str, Sequence[str], None] = "20260819000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No-op when no delimiter-shaped rows exist. Rewrites any that do so the
    # frontend can drop separator parsing without dumping documents onto the
    # rating card (issue #85).
    migrate_separator_questions(op.get_bind())


def downgrade() -> None:
    # Cannot distinguish migrated rows from originally parent-shaped ones, so
    # this does not re-concatenate. Rolling back the frontend change is what
    # restores delimiter rendering if needed.
    pass
