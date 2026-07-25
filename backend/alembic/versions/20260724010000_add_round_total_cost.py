"""add experiment_rounds total_cost column

Revision ID: 20260724010000
Revises: 20260724000000
Create Date: 2026-07-24 01:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260724010000"
down_revision: Union[str, Sequence[str], None] = "20260724000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Prolific's own `total_cost` for the round's study, in the workspace
    # currency's minor units (e.g. cents/pence). NULL until the round has been
    # synced from Prolific at least once. Experiment spend = sum over rounds.
    op.add_column(
        "experiment_rounds",
        sa.Column("total_cost", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("experiment_rounds", "total_cost")
