"""The migrations on Postgres."""

import threading

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from stateous.control.store import Store, tables
from stateous.control.store.migrations import BASELINE, VERSION_TABLE, head, upgrade_from_empty
from stateous.control.store.store import database_url

from ..conftest import fresh_postgres_database, needs_postgres

pytestmark = needs_postgres


def test_the_migrations_build_exactly_the_tables_the_code_declares() -> None:
    engine = create_engine(database_url(fresh_postgres_database()))
    upgrade_from_empty(engine)
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"version_table": VERSION_TABLE, "compare_server_default": True}
        )
        assert compare_metadata(context, tables.Base.metadata) == []
    engine.dispose()


def test_two_processes_migrating_at_once_both_succeed() -> None:
    url = fresh_postgres_database()
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def open_it() -> None:
        barrier.wait()
        try:
            Store(url).close()
        except BaseException as error:  # collected and checked below
            errors.append(error)

    threads = [threading.Thread(target=open_it) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    engine = create_engine(database_url(url))
    with engine.connect() as connection:
        versions = connection.execute(text(f"SELECT version_num FROM {VERSION_TABLE}"))
        assert versions.scalars().all() == [head()]
    engine.dispose()


def test_a_database_at_the_baseline_gets_the_claims_table_and_keeps_its_rows() -> None:
    url = fresh_postgres_database()
    engine = create_engine(database_url(url))
    upgrade_from_empty(engine, revision=BASELINE)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tokens (token, state, status, visit, context, entered_at) "
                "VALUES ('T-1', 'coding', 'active', 1, '{}', '2026-10-06 09:00:00')"
            )
        )
    engine.dispose()

    Store(url).close()

    engine = create_engine(database_url(url))
    with engine.connect() as connection:
        assert "claims" in inspect(connection).get_table_names()
        versions = connection.execute(text(f"SELECT version_num FROM {VERSION_TABLE}"))
        assert versions.scalars().all() == [head()]
        tokens = connection.execute(text("SELECT token FROM tokens"))
        assert tokens.scalars().all() == ["T-1"]
    engine.dispose()
