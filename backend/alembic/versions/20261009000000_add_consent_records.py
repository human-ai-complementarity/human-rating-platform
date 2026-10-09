"""add consent records

One row per rater recording their agreement to the consent statement: when,
and the exact text shown (plus which source file it came from). Raters from
before this migration have no row; reports read that as "not recorded", never
as "declined".

Revision ID: 20261009000000
Revises: 20260929055545
Create Date: 2026-10-09 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261009000000"
down_revision: Union[str, Sequence[str], None] = "20260929055545"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "consent_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "rater_id",
            sa.Integer(),
            sa.ForeignKey("raters.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "experiment_id",
            sa.Integer(),
            sa.ForeignKey("experiments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("prolific_id", sa.String(64), nullable=False),
        sa.Column("bundle", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("rendered_text", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_preview", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_consent_records_experiment_id", "consent_records", ["experiment_id"])


def downgrade() -> None:
    op.drop_index("ix_consent_records_experiment_id", table_name="consent_records")
    op.drop_table("consent_records")
