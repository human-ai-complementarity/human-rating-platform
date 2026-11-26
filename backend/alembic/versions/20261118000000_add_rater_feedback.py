"""add rater feedback column

Free-text feedback a rater may leave on the optional screen shown after
their last question. NULL when they skipped it.

Revision ID: 20261118000000
Revises: 20261107000000
Create Date: 2026-11-18 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261118000000"
down_revision: Union[str, Sequence[str], None] = "20261107000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("raters", sa.Column("feedback", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("raters", "feedback")
