"""The first schema of Stateous.

Revision ID: 0001
Revises: (none)
Create Date: 2026-10-06 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make every table and index, and the control plane's one row (notices off)."""
    op.create_table(
        "control_plane",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("structure", sa.String(length=80), nullable=True),
        sa.Column("checksum", sa.String(length=80), nullable=True),
        sa.Column("serving", sa.String(length=256), nullable=True),
        sa.Column("since", sa.DateTime(), nullable=True),
        sa.Column("scope", sa.JSON(), nullable=True),
        sa.Column("notices", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    control_plane = sa.table(
        "control_plane", sa.column("id", sa.Integer()), sa.column("notices", sa.Boolean())
    )
    op.bulk_insert(control_plane, [{"id": 1, "notices": False}])
    op.create_table(
        "replicas",
        sa.Column("holder", sa.String(length=256), nullable=False),
        sa.Column("structure", sa.String(length=80), nullable=False),
        sa.Column("checksum", sa.String(length=80), nullable=False),
        sa.Column("scope", sa.String(length=512), nullable=False),
        sa.Column("seen", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("holder"),
    )
    op.create_table(
        "claims",
        sa.Column("supervisor", sa.String(length=256), nullable=False),
        sa.Column("request", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("supervisor"),
    )
    op.create_table(
        "ledger",
        sa.Column("token", sa.String(length=256), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=256), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("token", "seq"),
    )
    op.create_index("ledger_key", "ledger", ["token", "key"], unique=True)
    op.create_table(
        "notices",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("family", sa.String(length=256), nullable=False),
        sa.Column("token", sa.String(length=256), nullable=False),
        sa.Column("parent", sa.String(length=256), nullable=True),
        sa.Column("workflow", sa.String(length=256), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("event", sa.String(length=256), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("state", sa.String(length=256), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index("notices_family", "notices", ["family", "id"], unique=False)
    op.create_table(
        "memory",
        sa.Column("token", sa.String(length=256), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("token", "state", "key"),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("token", sa.String(length=256), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("visit", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("claimed_by", sa.String(length=256), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("queued_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("tasks_queue", "tasks", ["status", "state"], unique=False)
    op.create_table(
        "timers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("token", sa.String(length=256), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("visit", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("set_at", sa.DateTime(), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("timers_due", "timers", ["status", "due_at"], unique=False)
    op.create_table(
        "tokens",
        sa.Column("token", sa.String(length=256), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("visit", sa.Integer(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("entered_at", sa.DateTime(), nullable=False),
        sa.Column("parent", sa.String(length=256), nullable=True),
        sa.Column("workflow", sa.String(length=64), nullable=True),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("token"),
    )
    op.create_table(
        "token_labels",
        sa.Column("token", sa.String(length=256), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=256), nullable=False),
        sa.PrimaryKeyConstraint("token", "key"),
    )
    op.create_index("token_labels_pair", "token_labels", ["key", "value"], unique=False)
    op.create_index("tokens_folded", "tokens", [sa.text("lower(token)")], unique=True)
    op.create_index("tokens_parent", "tokens", ["parent"], unique=False)


def downgrade() -> None:
    """Drop everything that `upgrade` made."""
    op.drop_index("token_labels_pair", table_name="token_labels")
    op.drop_table("token_labels")
    op.drop_index("tokens_parent", table_name="tokens")
    op.drop_index("tokens_folded", table_name="tokens")
    op.drop_table("tokens")
    op.drop_index("timers_due", table_name="timers")
    op.drop_table("timers")
    op.drop_index("tasks_queue", table_name="tasks")
    op.drop_table("tasks")
    op.drop_table("memory")
    op.drop_index("notices_family", table_name="notices")
    op.drop_table("notices")
    op.drop_index("ledger_key", table_name="ledger")
    op.drop_table("ledger")
    op.drop_table("claims")
    op.drop_table("replicas")
    op.drop_table("control_plane")
