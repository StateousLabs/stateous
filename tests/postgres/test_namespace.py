"""Namespaces on Postgres: each workflow's tables in their own schema."""

import threading
from datetime import UTC, datetime

from sqlalchemy import create_engine, inspect

from stateous.control.engine import Engine
from stateous.control.graph import Graph
from stateous.control.store import Store
from stateous.control.store.store import database_url

from ..conftest import fresh_postgres_database, needs_postgres
from ..unit.control.support import sdlc

pytestmark = needs_postgres

TICKET = {"repo": "acme/api"}


def tables_in(url: str, schema: str) -> set[str]:
    engine = create_engine(database_url(url))
    try:
        return set(inspect(engine).get_table_names(schema=schema))
    finally:
        engine.dispose()


def test_the_tables_go_in_the_namespace() -> None:
    url = fresh_postgres_database()

    Store(url, namespace="plan_tasks").close()

    assert {"tokens", "ledger", "notices", "stateous_version"} <= tables_in(url, "plan_tasks")
    assert tables_in(url, "public") == set()


def test_the_default_namespace_is_stateous() -> None:
    url = fresh_postgres_database()

    store = Store(url)
    try:
        assert store.namespace == "stateous"
    finally:
        store.close()

    assert "tokens" in tables_in(url, "stateous")
    assert tables_in(url, "public") == set()


def test_two_namespaces_do_not_share_tokens_or_locks() -> None:
    url = fresh_postgres_database()
    store_a = Store(url, namespace="a", hold=True)
    store_b = Store(url, namespace="b", hold=True)  # both hold their serving hold at once
    try:
        engine_a = Engine(store_a, Graph(sdlc()))
        engine_b = Engine(store_b, Graph(sdlc()))
        engine_a.start("T-1", {"repo": "acme/a"})
        engine_b.start("T-1", {"repo": "acme/b"})

        assert engine_a.get("T-1").context["repo"] == "acme/a"
        assert engine_b.get("T-1").context["repo"] == "acme/b"
        with store_a.transaction(datetime.now(UTC)) as tx:
            assert tx.lock_token("T-1")
            grant = engine_b.claim("sup", ["coding"])  # a's lock does not block b
        assert grant is not None
        assert grant.token == "T-1"
    finally:
        store_a.close()
        store_b.close()


def test_two_namespaces_migrate_at_once() -> None:
    url = fresh_postgres_database()
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def open_it(namespace: str) -> None:
        barrier.wait()
        try:
            Store(url, namespace=namespace).close()
        except BaseException as error:  # collected and checked below
            errors.append(error)

    threads = [threading.Thread(target=open_it, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert "tokens" in tables_in(url, "a")
    assert "tokens" in tables_in(url, "b")
