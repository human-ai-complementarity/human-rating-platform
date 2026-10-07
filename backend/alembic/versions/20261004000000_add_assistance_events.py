"""add_assistance_events

Revision ID: 20261004000000
Revises: 20260924000000
Create Date: 2026-10-04 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261004000000"
down_revision: Union[str, Sequence[str], None] = "20260924000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Backfills existing rows to 1: every existing session is on its start step.
    op.add_column(
        "assistance_sessions",
        sa.Column("turn", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.create_table(
        "assistance_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("assistance_session_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("step_type", sa.String(length=32), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["assistance_session_id"], ["assistance_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_assistance_events_assistance_session_id",
        "assistance_events",
        ["assistance_session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_assistance_events_assistance_session_id", table_name="assistance_events")
    op.drop_table("assistance_events")
    op.drop_column("assistance_sessions", "turn")
