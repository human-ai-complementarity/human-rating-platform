"""add_question_exclusion_key

Questions sharing an exclusion_key are related, so the selector serves a
rater at most one of them (#157). Nullable: unkeyed questions behave as
before.

Revision ID: 20261016000000
Revises: 20261011000000
Create Date: 2026-10-16 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261016000000"
down_revision: Union[str, Sequence[str], None] = "20261011000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "questions",
        sa.Column("exclusion_key", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("questions", "exclusion_key")
