"""add_datasets

Revision ID: 20260813000000
Revises: 20260729221924
Create Date: 2026-08-13 19:35:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260813000000"
down_revision: Union[str, Sequence[str], None] = "20260729221924"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "datasets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("waves", sa.Text(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Case-insensitive uniqueness: "SWE-bench" and "swe-bench" are the same
    # dataset. The service layer checks first for a clean 409; this index is
    # the backstop against races.
    op.create_index(
        "uq_datasets_name_lower",
        "datasets",
        [sa.text("lower(name)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_datasets_name_lower", table_name="datasets")
    op.drop_table("datasets")
