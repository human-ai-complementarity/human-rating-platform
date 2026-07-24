"""add experiment archived_at column

Revision ID: 20260724000000
Revises: 20260703000000
Create Date: 2026-07-24 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260724000000"
down_revision: Union[str, Sequence[str], None] = "20260703000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Soft-archive flag, orthogonal to `status`. NULL = active (default),
    # non-null timestamp = archived. No backfill: all existing experiments
    # start active.
    op.add_column(
        "experiments",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("experiments", "archived_at")
