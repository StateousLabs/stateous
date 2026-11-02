"""A store on an engine that the developer gives."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text

from stateous.control.store import SchemaNotReady, Store
from stateous.control.store.store import (
    POSTGRES_LOCK_WAIT,
    POSTGRES_STATEMENT_WITHIN,
    database_url,
)

from ..conftest import fresh_postgres_database, needs_postgres

pytestmark = needs_postgres


def test_a_given_engine_is_used_and_not_disposed() -> None:
    given = create_engine(database_url(fresh_postgres_database()))
    try:
        store = Store(given, namespace="plan_tasks")
        store.close()

        with given.connect() as connection:
            assert connection.scalar(text("SELECT 1")) == 1
            assert connection.scalar(
                text("SELECT count(*) FROM pg_tables WHERE schemaname = 'plan_tasks'")
            )
    finally:
        given.dispose()


def test_a_given_engine_gets_the_time_limits() -> None:
    given = create_engine(database_url(fresh_postgres_database()))
    store = Store(given)
    try:
        with store.transaction(datetime.now(UTC)) as tx:
            session = tx._session  # pyright: ignore[reportPrivateUsage]
            statement = session.scalar(text("SHOW statement_timeout"))
            lock = session.scalar(text("SHOW lock_timeout"))
        assert statement == f"{POSTGRES_STATEMENT_WITHIN}s"
        assert lock == f"{POSTGRES_LOCK_WAIT}s"
    finally:
        store.close()
        given.dispose()


def test_an_unmigrated_namespace_names_the_namespace_in_the_command() -> None:
    given = create_engine(database_url(fresh_postgres_database()))
    try:
        with pytest.raises(SchemaNotReady, match="in namespace 'plan_tasks'; run: stateous "
                           "migrate --db URL --namespace plan_tasks"):  # fmt: skip
            Store(given, namespace="plan_tasks", migrate=False)
    finally:
        given.dispose()
