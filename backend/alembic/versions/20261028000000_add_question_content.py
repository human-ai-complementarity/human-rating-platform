"""add_question_content

A question may carry images (#114): `content` holds its stimulus as a JSON
list of text and image blocks. Nullable: text-only questions keep
`question_text` alone.

Revision ID: 20261028000000
Revises: 20261016000000
Create Date: 2026-10-28 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261028000000"
down_revision: Union[str, Sequence[str], None] = "20261016000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("questions", sa.Column("content", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("questions", "content")
