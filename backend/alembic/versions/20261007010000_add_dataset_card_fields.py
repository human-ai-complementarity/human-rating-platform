"""add_dataset_card_fields

Dataset cards (#96): study configuration declared once per dataset so a
study can be launched without retyping it at create, upload, PATCH and the
pilot form. Set by editing the dataset (POST/PATCH /admin/datasets); nothing
seeds these columns.

Scope is how a *study* is run. The dataset's own presentation (rater
instructions, prompt prefix/suffix, system prompt, Prolific pool) is not
here: inference-pipeline's DatasetCard owns it and stamps it into the
exported CSV/parquet, which the upload path applies to the experiment.

All columns are nullable — a card is filled in over time, and an unfinished
card is legal (it just cannot launch a study).

Revision ID: 20261007010000
Revises: 20261007000000
Create Date: 2026-10-07 01:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20261007010000"
down_revision: Union[str, Sequence[str], None] = "20261007000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (column, type) — order matches DatasetCardFields in schemas.py.
_CARD_COLUMNS = (
    ("card_external_study_name", sa.String(length=255)),
    ("card_internal_study_name", sa.String(length=255)),
    ("card_study_blurb", sa.Text()),
    ("card_estimated_completion_time", sa.Integer()),
    ("card_reward", sa.Integer()),
    ("card_num_ratings_per_question", sa.Integer()),
    ("card_study_label", sa.String(length=64)),
    ("card_screeners", sa.Text()),
)


def upgrade() -> None:
    for name, type_ in _CARD_COLUMNS:
        op.add_column("datasets", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_CARD_COLUMNS):
        op.drop_column("datasets", name)
