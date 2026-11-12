"""add_pilot_check_acknowledgements

The round columns recording which pilot check run a launch was made against
and who acknowledged launching past a missing, stale or failing one
(docs/pilot-check.md).

Revision ID: 20261107000000
Revises: 20261106000000
Create Date: 2026-11-04 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261107000000"
down_revision: Union[str, Sequence[str], None] = "20261106000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "experiment_rounds",
        sa.Column("pilot_check_run_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "experiment_rounds",
        sa.Column("pilot_check_acknowledged_by", sa.String(length=255), nullable=True),
    )
    op.create_foreign_key(
        "fk_experiment_rounds_pilot_check_run_id",
        "experiment_rounds",
        "pilot_check_runs",
        ["pilot_check_run_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_experiment_rounds_pilot_check_run_id", "experiment_rounds", type_="foreignkey"
    )
    op.drop_column("experiment_rounds", "pilot_check_acknowledged_by")
    op.drop_column("experiment_rounds", "pilot_check_run_id")
