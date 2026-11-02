"""The migrations on Postgres."""

import threading

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from stateous.control.store import Store, tables
from stateous.control.store.migrations import VERSION_TABLE, head, upgrade_from_empty
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
