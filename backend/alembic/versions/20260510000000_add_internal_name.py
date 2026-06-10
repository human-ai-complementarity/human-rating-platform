"""add_internal_name

Revision ID: 20260510000000
Revises: 20260505000000
Create Date: 2026-05-10 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260510000000"
down_revision: Union[str, Sequence[str], None] = "20260505000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "experiments",
        sa.Column("internal_name", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("experiments", "internal_name")
