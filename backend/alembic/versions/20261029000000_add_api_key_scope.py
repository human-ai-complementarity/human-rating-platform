"""add_api_key_scope

Revision ID: 20261029000000
Revises: 20261026000000
Create Date: 2026-10-29 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261029000000"
down_revision: Union[str, Sequence[str], None] = "20261026000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Every existing key becomes `read`: none gains /api/admin access by migrating.
    op.add_column(
        "api_keys",
        sa.Column("scope", sa.String(length=16), nullable=False, server_default=sa.text("'read'")),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "scope")
