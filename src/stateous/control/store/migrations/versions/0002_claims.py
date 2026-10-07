"""The claims table: the last claim of each supervisor, for a repeat after a lost answer.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make the claims table. It starts empty."""
    op.create_table(
        "claims",
        sa.Column("supervisor", sa.String(length=256), nullable=False),
        sa.Column("request", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("supervisor"),
    )


def downgrade() -> None:
    """Drop the claims table."""
    op.drop_table("claims")
