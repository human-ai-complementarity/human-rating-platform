"""add rater timed_out column

A timed-out session and a finished one were indistinguishable afterwards —
both set is_active = false and stamped session_end — so the rate at which the
clock cuts raters off could not be measured. See issue #102.

Revision ID: 20260913010000
Revises: 20260913000000
Create Date: 2026-09-13 01:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260913010000"
down_revision: Union[str, Sequence[str], None] = "20260913000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows get false. That is a claim we cannot verify either way for
    # sessions that ended before this column existed, so read historical zeros
    # as "unknown" rather than "did not time out".
    op.add_column(
        "raters",
        sa.Column("timed_out", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("raters", "timed_out")
