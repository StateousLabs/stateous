"""Each Postgres connection and statement has a time limit (docs/design.md, section 7).
A request that waits for a lock that another connection holds answers 503
`database_busy` after the lock wait, and does not wait for ever."""

import threading
import time
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from starlette.testclient import TestClient

from stateous.control.api import create_app
from stateous.control.engine import Engine
from stateous.control.graph import Graph
from stateous.control.store import Store
from stateous.control.store.store import POSTGRES_LOCK_WAIT, TOKEN_LOCK, database_url

from ..conftest import fresh_postgres_database, needs_postgres
from ..unit.control.support import TICKET, sdlc

pytestmark = needs_postgres

ANSWER_WITHIN = 15.0
"""Seconds for the answer: the lock wait (10 s), and a margin."""


@pytest.fixture
def url() -> str:
    return fresh_postgres_database()


@pytest.fixture
def store(url: str) -> Iterator[Store]:
    store = Store(url)
    yield store
    store.close()


def test_each_session_has_the_statement_and_lock_limits(store: Store) -> None:
    """The migrations ran on the same engine; its sessions have the limits."""
    with store.engine.connect() as connection:
        assert connection.scalar(text("SHOW statement_timeout")) == "30s"
        assert connection.scalar(text("SHOW lock_timeout")) == "10s"


def test_a_request_that_waits_for_a_held_lock_answers_database_busy(url: str, store: Store) -> None:
    engine = Engine(store, Graph(sdlc()))
    engine.start("T-1", TICKET)
    http = TestClient(create_app(engine), raise_server_exceptions=False)
    # Another connection, with no time limits, holds the lock of T-1's family.
    other = create_engine(database_url(url), isolation_level="AUTOCOMMIT")
    answers: list[tuple[int, str, float]] = []

    def cancel() -> None:
        started = time.monotonic()
        answer = http.post("/tokens/T-1/cancel")
        answers.append((answer.status_code, answer.json().get("code"), time.monotonic() - started))

    try:
        with other.connect() as holder:
            holder.execute(
                text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"),
                {"key": TOKEN_LOCK + "t-1"},
            )
            # A daemon thread: if the request waits for ever, the test fails and the
            # run goes on. The teardown drops the database, and that ends the wait.
            thread = threading.Thread(target=cancel, daemon=True)
            thread.start()
            thread.join(timeout=ANSWER_WITHIN)
            holder.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                {"key": TOKEN_LOCK + "t-1"},
            )
    finally:
        other.dispose()

    assert answers, f"the request did not answer in {ANSWER_WITHIN:.0f} s"
    status, code, took = answers[0]
    assert (status, code) == (503, "database_busy")
    assert took >= POSTGRES_LOCK_WAIT - 1  # it waited for the lock, once
    assert engine.get("T-1").status == "active"
