"""add experiment status column

Revision ID: 20260703000000
Revises: 20260701000000
Create Date: 2026-07-03 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260703000000"
down_revision: Union[str, Sequence[str], None] = "20260701000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "experiments",
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="DRAFT",
        ),
    )
    # Backfill: experiments with any main round (round_number >= 1) are already
    # past the draft phase, so their config is effectively locked. Mark them
    # LAUNCH. Nothing is auto-marked FINISHED — that requires an explicit admin
    # action, and there's no historical signal for it.
    op.execute(
        """
        UPDATE experiments
        SET status = 'LAUNCH'
        WHERE id IN (
            SELECT DISTINCT experiment_id
            FROM experiment_rounds
            WHERE round_number >= 1
        )
        """
    )


def downgrade() -> None:
    op.drop_column("experiments", "status")
