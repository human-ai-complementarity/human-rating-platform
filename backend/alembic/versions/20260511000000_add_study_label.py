"""add_study_label

Revision ID: 20260511000000
Revises: 20260510000000
Create Date: 2026-05-11 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260511000000"
down_revision: Union[str, Sequence[str], None] = "20260510000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "experiment_rounds",
        sa.Column("study_label", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("experiment_rounds", "study_label")
