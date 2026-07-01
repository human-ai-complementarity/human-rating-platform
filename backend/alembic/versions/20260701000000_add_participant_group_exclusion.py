"""add participant group exclusion columns

Revision ID: 20260701000000
Revises: 20260618000000
Create Date: 2026-07-01 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260701000000"
down_revision: Union[str, Sequence[str], None] = "20260618000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "experiments",
        sa.Column("prolific_participant_group_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "experiment_rounds",
        sa.Column("excluded_experiment_ids", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("experiment_rounds", "excluded_experiment_ids")
    op.drop_column("experiments", "prolific_participant_group_id")
