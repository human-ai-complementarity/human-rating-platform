"""add_pilot_check_runs

One row per admin-triggered pilot check of an experiment, plus the round
columns recording which run a launch was made against and who acknowledged
launching past a missing, stale or failing one (docs/pilot-check.md).

Revision ID: 20261103000000
Revises: 20261016000000
Create Date: 2026-10-28 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261103000000"
down_revision: Union[str, Sequence[str], None] = "20261029000000"
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
        sa.Column("stages", sa.Text(), nullable=False, server_default="[]"),
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
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pilot_check_runs_experiment_id", "pilot_check_runs", ["experiment_id"], unique=False
    )
    op.add_column(
        "experiment_rounds",
        sa.Column("pilot_check_run_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "experiment_rounds",
        sa.Column("pilot_check_acknowledged_by", sa.String(length=255), nullable=True),
    )
    op.create_foreign_key(
        "fk_experiment_rounds_pilot_check_run_id",
        "experiment_rounds",
        "pilot_check_runs",
        ["pilot_check_run_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_experiment_rounds_pilot_check_run_id", "experiment_rounds", type_="foreignkey"
    )
    op.drop_column("experiment_rounds", "pilot_check_acknowledged_by")
    op.drop_column("experiment_rounds", "pilot_check_run_id")
    op.drop_index("ix_pilot_check_runs_experiment_id", table_name="pilot_check_runs")
    op.drop_table("pilot_check_runs")
