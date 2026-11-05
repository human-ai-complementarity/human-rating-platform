"""add experiment render_math column

Revision ID: 20261104000000
Revises: 20261029000000
Create Date: 2026-11-04 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261104000000"
down_revision: Union[str, Sequence[str], None] = "20261029000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "experiments",
        sa.Column("render_math", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("experiments", "render_math")
