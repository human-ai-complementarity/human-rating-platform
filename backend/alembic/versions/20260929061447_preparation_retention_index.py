"""preparation_retention_index

Revision ID: 20260929061447
Revises: 20260929072607
Create Date: 2026-09-29 06:14:48.548895

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260929061447"
down_revision: Union[str, Sequence[str], None] = "20260929072607"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_assistance_preparations_deadline_at", "assistance_preparations", ["deadline_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_assistance_preparations_deadline_at", table_name="assistance_preparations")
