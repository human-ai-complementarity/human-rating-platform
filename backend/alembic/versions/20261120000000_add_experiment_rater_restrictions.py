"""add experiment block_copy_paste and forbid_outside_tools columns

Revision ID: 20261120000000
Revises: 20261118000000
Create Date: 2026-11-20 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261120000000"
down_revision: Union[str, Sequence[str], None] = "20261118000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = ("block_copy_paste", "forbid_outside_tools")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            "experiments",
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("experiments", name)
