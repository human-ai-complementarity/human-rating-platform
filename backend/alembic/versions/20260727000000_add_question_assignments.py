"""add question_assignments table

Revision ID: 20260727000000
Revises: 20260724010000
Create Date: 2026-07-27 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260727000000"
down_revision: Union[str, Sequence[str], None] = "20260724010000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # In-flight reservation of a rating slot: created when a question is
    # served to a rater, completed when the rating is submitted. Live rows
    # (completed_at IS NULL and expires_at in the future) count toward the
    # question's rating target during selection.
    op.create_table(
        "question_assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "question_id",
            sa.Integer(),
            sa.ForeignKey("questions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "rater_id",
            sa.Integer(),
            sa.ForeignKey("raters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("question_id", "rater_id", name="uq_assignment_question_rater"),
    )
    op.create_index(
        "ix_question_assignments_question_id",
        "question_assignments",
        ["question_id"],
    )
    op.create_index(
        "ix_question_assignments_rater_id",
        "question_assignments",
        ["rater_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_question_assignments_rater_id", table_name="question_assignments")
    op.drop_index("ix_question_assignments_question_id", table_name="question_assignments")
    op.drop_table("question_assignments")
