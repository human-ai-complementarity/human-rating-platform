"""add_experiment_groups

Revision ID: 20260815000000
Revises: 20260813000000
Create Date: 2026-08-15 18:45:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260815000000"
down_revision: Union[str, Sequence[str], None] = "20260813000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "experiment_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("wave", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset_id", "wave", name="uq_experiment_groups_dataset_wave"),
    )
    op.create_index(
        "uq_experiment_groups_dataset_name_lower",
        "experiment_groups",
        ["dataset_id", sa.text("lower(name)")],
        unique=True,
    )
    op.add_column(
        "experiments",
        sa.Column("group_id", sa.Integer(), nullable=True),
    )
    op.create_index(op.f("ix_experiments_group_id"), "experiments", ["group_id"], unique=False)
    op.create_foreign_key(
        "fk_experiments_group_id_experiment_groups",
        "experiments",
        "experiment_groups",
        ["group_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_experiments_group_id_experiment_groups",
        "experiments",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_experiments_group_id"), table_name="experiments")
    op.drop_column("experiments", "group_id")
    op.drop_index("uq_experiment_groups_dataset_name_lower", table_name="experiment_groups")
    op.drop_table("experiment_groups")
