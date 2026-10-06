"""One control plane for each database, on Postgres: the advisory lock."""

import threading
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect, text

import stateous.control.store.store as store_module
from stateous.control.engine import Engine, Token
from stateous.control.graph import Graph
from stateous.control.store import DatabaseHeld, Store
from stateous.control.store.store import SERVING_LOCK, database_url

from ..conftest import fresh_postgres_database, needs_postgres
from ..unit.control.support import TICKET, sdlc

pytestmark = needs_postgres


@pytest.fixture
def url() -> str:
    return fresh_postgres_database()


def _kill_serving_backend(store: Store, url: str) -> None:
    """End the Postgres backend of the serving lock, as a database restart or a
    dropped network connection does: its session ends without `close()`."""
    serving = store._serving  # pyright: ignore[reportPrivateUsage]
    assert serving is not None
    pid = serving.scalar(text("SELECT pg_backend_pid()"))
    admin = create_engine(database_url(url), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
    admin.dispose()


def test_a_second_store_is_refused_until_the_first_stops(url: str) -> None:
    first = Store(url, hold=True)
    try:
        with pytest.raises(DatabaseHeld, match="another control plane serves this database"):
            Store(url, hold=True)
    finally:
        first.close()

    Store(url, hold=True).close()


def test_a_refused_store_migrates_nothing(url: str) -> None:
    """The lock is taken by hand on a new, empty database. `Store(url, hold=True)`
    must refuse before it migrates anything."""
    holder = create_engine(database_url(url), isolation_level="AUTOCOMMIT")
    try:
        with holder.connect() as held:
            held.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                {"key": SERVING_LOCK},
            )
            with pytest.raises(DatabaseHeld):
                Store(url, hold=True)
            assert inspect(held).get_table_names() == []
    finally:
        holder.dispose()


def test_a_failed_migration_after_the_hold_releases_it(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_migrate = store_module.migrate
    calls = 0

    def fail_once(engine: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("the migration failed")
        real_migrate(engine)

    monkeypatch.setattr(store_module, "migrate", fail_once)

    with pytest.raises(RuntimeError, match="the migration failed"):
        Store(url, hold=True)

    other = Store(url)
    try:
        assert other.hold_serving()
    finally:
        other.close()


def test_a_lost_hold_is_taken_again_while_the_lock_is_free(url: str) -> None:
    holder = Store(url, hold=True)
    try:
        _kill_serving_backend(holder, url)

        assert holder.keep_serving()

        other = Store(url)
        try:
            assert not other.hold_serving()
        finally:
            other.close()
    finally:
        holder.close()


def test_a_lost_hold_is_not_taken_again_once_another_store_holds_it(url: str) -> None:
    holder = Store(url, hold=True)
    other = Store(url)
    try:
        _kill_serving_backend(holder, url)
        assert other.hold_serving()

        assert not holder.keep_serving()
    finally:
        holder.close()
        other.close()


def test_two_databases_can_each_be_held_at_once() -> None:
    first = Store(fresh_postgres_database(), hold=True)
    second = Store(fresh_postgres_database(), hold=True)
    first.close()
    second.close()


def test_a_token_named_like_the_serving_lock_does_not_wait_for_it(url: str) -> None:
    holder = Store(url, hold=True)
    other = Engine(Store(url), Graph(sdlc()))
    try:
        started: list[Token] = []
        thread = threading.Thread(target=lambda: started.append(other.start(SERVING_LOCK, TICKET)))
        thread.start()
        thread.join(timeout=5)

        assert not thread.is_alive(), "the start waited for the serving lock"
        assert started and started[0].token == SERVING_LOCK
    finally:
        holder.close()
        other.store.close()
