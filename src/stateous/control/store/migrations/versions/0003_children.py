"""Child tokens: each token's parent, and the child workflow of a child token.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add `parent` and `workflow` to tokens. Both stay empty for the tokens there are:
    each is a top token of the main workflow. Index `parent`, to find the children.
    Make `tokens_folded` unique: a child's entry and a top token's start take
    different locks, so only the database can refuse ids that differ only in case."""
    with op.batch_alter_table("tokens") as batch_op:
        batch_op.add_column(sa.Column("parent", sa.String(length=256), nullable=True))
        batch_op.add_column(sa.Column("workflow", sa.String(length=64), nullable=True))
    op.create_index("tokens_parent", "tokens", ["parent"], unique=False)
    op.drop_index("tokens_folded", table_name="tokens")
    op.create_index("tokens_folded", "tokens", [sa.text("lower(token)")], unique=True)


def downgrade() -> None:
    """Drop the index and the two columns. `tokens_folded` is not unique again."""
    op.drop_index("tokens_folded", table_name="tokens")
    op.drop_index("tokens_parent", table_name="tokens")
    with op.batch_alter_table("tokens") as batch_op:
        batch_op.drop_column("workflow")
        batch_op.drop_column("parent")
    op.create_index("tokens_folded", "tokens", [sa.text("lower(token)")], unique=False)
