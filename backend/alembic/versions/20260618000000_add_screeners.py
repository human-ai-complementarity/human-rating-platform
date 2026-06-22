"""add_screeners

Revision ID: 20260618000000
Revises: 20260617000000
Create Date: 2026-06-18 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260618000000"
down_revision: Union[str, Sequence[str], None] = "20260617000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "experiment_rounds",
        sa.Column("screeners", sa.String(length=256), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("experiment_rounds", "screeners")
