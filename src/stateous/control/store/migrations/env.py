"""Alembic's environment for Stateous's own tables.

Stateous runs it with a connection already open (`config.attributes["connection"]`);
contributors run it through `alembic.ini`, which names a database by URL.
"""

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool

from stateous.control.store import tables
from stateous.control.store.migrations import VERSION_TABLE

config = context.config


def run() -> None:
    """Migrate the connection Stateous handed over, or else the database `alembic.ini` names."""
    connection = config.attributes.get("connection")
    if isinstance(connection, Connection):
        _migrate(connection)
        return
    engine = engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with engine.begin() as own:
        _migrate(own)
    engine.dispose()


def _migrate(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=tables.Base.metadata,
        render_as_batch=True,  # SQLite cannot ALTER most things; batch mode rebuilds the table
        version_table=VERSION_TABLE,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run()
