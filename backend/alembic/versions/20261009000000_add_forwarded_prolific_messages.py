"""add_forwarded_prolific_messages

Revision ID: 20261009000000
Revises: 20260929054644
Create Date: 2026-10-06 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261009000000"
down_revision: Union[str, Sequence[str], None] = "20260929054644"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "forwarded_prolific_messages",
        sa.Column("prolific_message_id", sa.String(length=128), nullable=False),
        sa.Column(
            "forwarded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("prolific_message_id"),
    )


def downgrade() -> None:
    op.drop_table("forwarded_prolific_messages")
