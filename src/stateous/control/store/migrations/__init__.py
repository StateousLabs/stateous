"""Keeping a database's schema in step with this version of Stateous.

A new database is made from the models and stamped at the newest revision. A
database that is behind is upgraded in place, and keeps its data. A database that
this version cannot use is refused and left as it is.

The two common cases, a new database and one at the newest revision, never run
Alembic's environment, and the script folder is read once for each process.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, insert, inspect, text

from stateous.control.store import tables
from stateous.control.store.errors import ForeignDatabase

BASELINE = "0001"
"""The first revision."""

VERSION_TABLE = "stateous_version"
"""Where Stateous keeps its revision. It is its own table, so that an application
that uses Alembic in the same database keeps `alembic_version` for itself."""

ADDED: dict[str, str] = {"claims": "0002"}
"""The tables that a revision after the baseline added, and that revision. A
database at an older revision does not have them yet: the upgrade makes them."""

HERE = Path(__file__).parent
"""The package's migrations: env.py, the template, and versions/."""

MIGRATE_LOCK = "@stateous/migrate"
"""The advisory-lock key of `_lock`. A token's key starts with `token:`, so the two
can never be equal."""


def migrate(engine: Engine, *, script_location: Path | None = None) -> None:
    """Bring the database to this version's schema, and keep its data.

    `script_location` is for tests, which add revisions to a copy of the package's
    migrations."""
    location = script_location or HERE
    with engine.begin() as connection:
        _lock(connection)
        context = MigrationContext.configure(connection, opts={"version_table": VERSION_TABLE})
        current = context.get_current_revision()
        newest = _head(location)
        present = set(inspect(connection).get_table_names())
        if current is None:
            if _ours() & present:
                raise ForeignDatabase(
                    "this database has tables with the names Stateous uses, but no version "
                    "of Stateous; start a new database"
                )
            tables.Base.metadata.create_all(connection)
            _seed(connection)
            context.stamp(_scripts(location), newest)
            return
        if current not in _known(location):
            raise ForeignDatabase(
                f"this database was made by another version of Stateous (schema {current}); "
                "use that version, or start a new database"
            )
        if missing := sorted(_expected(location, current) - present):
            raise ForeignDatabase(
                f"this database has no {', '.join(missing)} table; start a new database"
            )
        if current != newest:
            command.upgrade(_config(location, connection), "head")


def head() -> str:
    """The newest revision this version of Stateous knows."""
    return _head(HERE)


def upgrade_from_empty(engine: Engine, *, revision: str = "head") -> None:
    """Every migration from nothing, never `create_all`: for the test that the
    migrations build exactly what the models declare."""
    with engine.begin() as connection:
        command.upgrade(_config(HERE, connection), revision)


def _seed(connection: Connection) -> None:
    """The rows a new database starts with: the control plane's one row. Revision
    0001 puts the same row in a database that is migrated instead."""
    connection.execute(insert(tables.ControlPlane).values(id=1))


def _config(location: Path, connection: Connection) -> Config:
    """Alembic's configuration, to run env.py on a connection that is already open."""
    config = Config()
    config.set_main_option("script_location", str(location))
    config.attributes["connection"] = connection
    return config


@cache
def _scripts(location: Path) -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(location))
    return ScriptDirectory.from_config(config)


@cache
def _head(location: Path) -> str:
    found = _scripts(location).get_current_head()
    assert found is not None, "the migrations have no head revision"
    return found


@cache
def _known(location: Path) -> frozenset[str]:
    return frozenset(script.revision for script in _scripts(location).walk_revisions())


@cache
def _expected(location: Path, revision: str) -> frozenset[str]:
    """The tables that a database at `revision` has: ours, less those that a later
    revision adds."""
    lineage = {script.revision for script in _scripts(location).iterate_revisions(revision, "base")}
    return _ours() - {table for table, added in ADDED.items() if added not in lineage}


@cache
def _ours() -> frozenset[str]:
    return frozenset(table.name for table in tables.Base.metadata.sorted_tables)


def _lock(connection: Connection) -> None:
    """On Postgres, one process migrates at a time. A second control plane that
    starts at the same moment waits, then finds nothing to do."""
    if connection.dialect.name == "postgresql":
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": MIGRATE_LOCK},
        )
