"""add_pilot_check_screenshots

Screenshots of rater screens that a pilot check finding names, for the
admin to open from the finding (docs/pilot-check.md).

Revision ID: 20261113000000
Revises: 20261107000000
Create Date: 2026-11-13 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261113000000"
down_revision: Union[str, Sequence[str], None] = "20261107000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pilot_check_screenshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("pilot_check_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("screen", sa.String(length=32), nullable=False),
        sa.Column("png", sa.LargeBinary(), nullable=False),
    )
    op.create_index("ix_pilot_check_screenshots_run_id", "pilot_check_screenshots", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_pilot_check_screenshots_run_id", table_name="pilot_check_screenshots")
    op.drop_table("pilot_check_screenshots")
