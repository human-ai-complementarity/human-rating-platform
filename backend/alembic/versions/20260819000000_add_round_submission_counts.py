"""add experiment_rounds submission count columns

Revision ID: 20260819000000
Revises: 20260813000000
Create Date: 2026-08-19 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260819000000"
down_revision: Union[str, Sequence[str], None] = "20260813000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Prolific submission counts for the round's study, tallied from the
    # study's submission statuses. NULL until the round has been synced from
    # Prolific at least once. Places still open = places_requested minus both.
    op.add_column(
        "experiment_rounds",
        sa.Column("submissions_completed", sa.Integer(), nullable=True),
    )
    op.add_column(
        "experiment_rounds",
        sa.Column("submissions_in_progress", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("experiment_rounds", "submissions_in_progress")
    op.drop_column("experiment_rounds", "submissions_completed")
