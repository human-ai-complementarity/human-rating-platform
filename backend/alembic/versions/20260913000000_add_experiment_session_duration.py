"""add experiment session_duration_minutes column

Sessions used to be a hard-coded hour. Making the length per-experiment is
what unblocks long rating tasks (issue #102). Existing rows get 60 so nothing
about their behaviour changes.

Revision ID: 20260913000000
Revises: 20260901000000
Create Date: 2026-09-13 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260913000000"
down_revision: Union[str, Sequence[str], None] = "20260901000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default carries the backfill for existing rows and keeps the
    # column NOT NULL for inserts that predate the application change.
    op.add_column(
        "experiments",
        sa.Column(
            "session_duration_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("60"),
        ),
    )


def downgrade() -> None:
    op.drop_column("experiments", "session_duration_minutes")
