"""add rater terms and consent records

Consent and debrief statements are versioned files in a terms source (a GCS
prefix in production, backend/rater_terms locally). `terms_statements`
archives every version an experiment pins so a consent record can name the
exact text a rater agreed to; `consent_records` holds one agreement per rater.
Experiments choose a bundle, may carry a Prolific content warning, and pin
their statement versions at first publish.

Revision ID: 20261009000000
Revises: 20261007000000
Create Date: 2026-10-09 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261009000000"
down_revision: Union[str, Sequence[str], None] = "20261007000000"
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

    # Existing experiments become standard, warning-free studies; nothing is
    # pinned for them until they publish a round.
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

    # Raters from before this migration have no record; reports show them as
    # "not recorded", never as declined.
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
        sa.Column(
            "statement_id",
            sa.Integer(),
            sa.ForeignKey("terms_statements.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("rendered_text", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_preview", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_consent_records_experiment_id", "consent_records", ["experiment_id"])


def downgrade() -> None:
    op.drop_index("ix_consent_records_experiment_id", table_name="consent_records")
    op.drop_table("consent_records")
    op.drop_column("experiments", "debrief_statement_id")
    op.drop_column("experiments", "consent_statement_id")
    op.drop_column("experiments", "terms_bundle")
    op.drop_column("experiments", "content_warning_details")
    op.drop_column("experiments", "content_warning")
    op.drop_table("terms_statements")
