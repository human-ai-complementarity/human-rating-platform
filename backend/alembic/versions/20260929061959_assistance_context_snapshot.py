"""assistance_context_snapshot

Revision ID: 20260929061959
Revises: 20260929061447
Create Date: 2026-09-29 06:20:00.472438

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260929061959"
down_revision: Union[str, Sequence[str], None] = "20260929061447"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("assistance_sessions", sa.Column("context_snapshot", sa.Text(), nullable=True))
    op.add_column("assistance_sessions", sa.Column("outcome", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("assistance_sessions", "outcome")
    op.drop_column("assistance_sessions", "context_snapshot")
