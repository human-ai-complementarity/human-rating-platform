"""add_dataset_metadata

Revision ID: 20260617000000
Revises: 20260511000000
Create Date: 2026-06-17 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260617000000"
down_revision: Union[str, Sequence[str], None] = "20260511000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("experiments", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("experiments", sa.Column("system_prompt", sa.Text(), nullable=True))
    op.add_column("experiments", sa.Column("human_prompt_prefix", sa.Text(), nullable=True))
    op.add_column("experiments", sa.Column("human_prompt_suffix", sa.Text(), nullable=True))
    op.add_column(
        "experiments",
        sa.Column("prolific_pool", sa.String(length=255), nullable=True),
    )
    op.add_column("uploads", sa.Column("dataset_meta", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("uploads", "dataset_meta")
    op.drop_column("experiments", "prolific_pool")
    op.drop_column("experiments", "human_prompt_suffix")
    op.drop_column("experiments", "human_prompt_prefix")
    op.drop_column("experiments", "system_prompt")
    op.drop_column("experiments", "description")
