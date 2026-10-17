"""add study flags and pinned terms

Experiments carry a list of study flags (sensitive, explicit, deception) with
content-warning details, and pin their consent/debrief statement versions at
first publish. `terms_statements` archives every pinned version so a
published experiment keeps serving the text it went live with. Existing
experiments become unflagged studies with nothing pinned until they publish.

Revision ID: 20261012000000
Revises: 20261011000000
Create Date: 2026-10-12 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261012000000"
down_revision: Union[str, Sequence[str], None] = "20261011000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "terms_statements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("document", "version", name="uq_terms_statement_version"),
    )

    op.add_column(
        "experiments",
        sa.Column("study_flags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column("experiments", sa.Column("content_warning_details", sa.Text(), nullable=True))
    op.add_column(
        "experiments",
        sa.Column(
            "consent_statement_id",
            sa.Integer(),
            sa.ForeignKey("terms_statements.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column(
        "experiments",
        sa.Column(
            "debrief_statement_id",
            sa.Integer(),
            sa.ForeignKey("terms_statements.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("experiments", "debrief_statement_id")
    op.drop_column("experiments", "consent_statement_id")
    op.drop_column("experiments", "content_warning_details")
    op.drop_column("experiments", "study_flags")
    op.drop_table("terms_statements")
