"""add content warnings and pinned terms

Experiments choose a terms bundle, may carry a Prolific content warning with
details, and pin their consent/debrief statement versions at first publish.
`terms_statements` archives every pinned version so a published experiment
keeps serving the text it went live with. Existing experiments become
standard, warning-free studies with nothing pinned until they publish.

Revision ID: 20261010000000
Revises: 20261009000000
Create Date: 2026-10-10 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261010000000"
down_revision: Union[str, Sequence[str], None] = "20261009000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "terms_statements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("bundle", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
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
        sa.UniqueConstraint("bundle", "kind", "version", name="uq_terms_statement_version"),
    )

    op.add_column(
        "experiments",
        sa.Column(
            "content_warning", sa.String(16), nullable=False, server_default=sa.text("'none'")
        ),
    )
    op.add_column("experiments", sa.Column("content_warning_details", sa.Text(), nullable=True))
    op.add_column(
        "experiments",
        sa.Column(
            "terms_bundle", sa.String(64), nullable=False, server_default=sa.text("'standard'")
        ),
    )
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
    op.drop_column("experiments", "terms_bundle")
    op.drop_column("experiments", "content_warning_details")
    op.drop_column("experiments", "content_warning")
    op.drop_table("terms_statements")
