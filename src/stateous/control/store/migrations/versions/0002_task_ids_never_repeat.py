"""A task id never repeats on SQLite: `tasks` gets AUTOINCREMENT.

A purge deletes tasks. Without AUTOINCREMENT, SQLite gives the id of the last row
again once that row is deleted, so a new task could get the id of a purged task
whose supervisor still reports on it. Postgres never gives an id again: nothing
changes there.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-23 09:00:00
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """On SQLite, make `tasks` again with AUTOINCREMENT, with its rows and its index."""
    _rebuild_tasks(autoincrement=True)


def downgrade() -> None:
    """On SQLite, make `tasks` again without AUTOINCREMENT."""
    _rebuild_tasks(autoincrement=False)


def _rebuild_tasks(*, autoincrement: bool) -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    with op.batch_alter_table(
        "tasks", recreate="always", table_kwargs={"sqlite_autoincrement": autoincrement}
    ):
        pass
