"""add experiment is_markdown column

Revision ID: 20260922000000
Revises: 20260901000000
Create Date: 2026-09-22 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260922000000"
down_revision: Union[str, Sequence[str], None] = "20260901000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing experiments keep plain-text rendering.
    op.add_column(
        "experiments",
        sa.Column("is_markdown", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("experiments", "is_markdown")
