"""add_question_parent_id

Revision ID: 20260505000000
Revises: 20260318000000
Create Date: 2026-05-05 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260505000000"
down_revision: Union[str, Sequence[str], None] = "20260318000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "questions",
        sa.Column("parent_question_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_questions_parent_question_id",
        "questions",
        "questions",
        ["parent_question_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # Partial index over non-null values only: most rows are standalone
    # questions (parent_question_id IS NULL) so there's no reason to carry
    # them in the index. Speeds up parent_question_ids_subquery() and the
    # FK-cascade lookup when a parent row is deleted.
    op.create_index(
        "ix_questions_parent_question_id",
        "questions",
        ["parent_question_id"],
        postgresql_where=sa.text("parent_question_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_questions_parent_question_id", table_name="questions")
    op.drop_constraint(
        "fk_questions_parent_question_id",
        "questions",
        type_="foreignkey",
    )
    op.drop_column("questions", "parent_question_id")
