"""add_pilot_check_runs

One row per admin-triggered pilot check of an experiment (docs/pilot-check.md).

Revision ID: 20261106000000
Revises: 20261105000000
Create Date: 2026-10-28 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261106000000"
down_revision: Union[str, Sequence[str], None] = "20261105000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pilot_check_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("experiment_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=True),
        sa.Column("stage_detail", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.String(length=255), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("terms_ref", sa.String(length=128), nullable=True),
        sa.Column("sample_question_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("findings", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("assistance_steps", sa.Text(), nullable=True),
        sa.Column("judge_model", sa.String(length=128), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pilot_check_runs_experiment_id", "pilot_check_runs", ["experiment_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_pilot_check_runs_experiment_id", table_name="pilot_check_runs")
    op.drop_table("pilot_check_runs")
