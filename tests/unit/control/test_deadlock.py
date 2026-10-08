"""A public engine call that the database stops for a deadlock runs once more.

Postgres stops one of two transactions that wait for each other: a heartbeat that
renews leases in the order of task ids, and a cascade that changes the tasks of
several children in the order of the workflows. SQLite cannot deadlock, so a fake
store raises the error that psycopg gives."""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime

import psycopg.errors
import pytest
from sqlalchemy.exc import OperationalError

from stateous.control.engine import Engine
from stateous.control.graph import Graph
from stateous.control.store import Store, Transaction

from ...parallel import DESIGN, SECURITY, TOP, parallel_document
from .support import Clock


class Stopping:
    """Make the store's next `times` transactions fail as they end, with `error`."""

    def __init__(self, store: Store, error: Exception, times: int) -> None:
        self.store = store
        self.real = store.transaction
        self.error = error
        self.times = times
        self.opened = 0

    @contextmanager
    def transaction(self, now: datetime) -> Generator[Transaction]:
        self.opened += 1
        with self.real(now) as tx:
            yield tx
            if self.times > 0:
                self.times -= 1
                raise self.error  # the real transaction rolls back


def database_error(error: Exception) -> OperationalError:
    """The error as SQLAlchemy raises it: the driver's error is its `orig`."""
    return OperationalError("UPDATE tasks SET lease_until = ...", {}, error)


def at_reviews(
    monkeypatch: pytest.MonkeyPatch, error: Exception, times: int
) -> tuple[Engine, Stopping]:
    """T-1 in `reviews`, with two children; then the next `times` transactions fail."""
    store = Store("sqlite://")
    engine = Engine(store, Graph(parallel_document()), clock=Clock())
    engine.start(TOP, {"repo": "acme/api"})
    engine.fire(TOP, "done", None)
    stopping = Stopping(store, error, times)
    monkeypatch.setattr(store, "transaction", stopping.transaction)
    return engine, stopping


@pytest.mark.parametrize(
    "driver_error",
    [
        psycopg.errors.DeadlockDetected("deadlock detected"),
        psycopg.errors.SerializationFailure("could not serialize access"),
    ],
)
def test_a_call_that_the_database_stops_for_a_deadlock_runs_once_more(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, driver_error: Exception
) -> None:
    engine, stopping = at_reviews(monkeypatch, database_error(driver_error), times=1)

    with caplog.at_level(logging.WARNING, logger="stateous.engine"):
        cancelled = engine.cancel(TOP)

    assert cancelled.status == "cancelled"
    assert stopping.opened == 2
    assert [engine.get(child).status for child in (SECURITY, DESIGN)] == ["cancelled"] * 2
    assert [e.kind for e in engine.history(TOP)].count("cancelled") == 1
    lines = [r.getMessage() for r in caplog.records if r.name == "stateous.engine"]
    assert len(lines) == 1
    assert "cancel" in lines[0]


def test_a_second_deadlock_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    error = database_error(psycopg.errors.DeadlockDetected("deadlock detected"))
    engine, stopping = at_reviews(monkeypatch, error, times=2)

    with pytest.raises(OperationalError):
        engine.cancel(TOP)

    assert stopping.opened == 2
    assert engine.get(TOP).status == "active"


@pytest.mark.parametrize(
    "driver_error",
    [
        psycopg.errors.AdminShutdown("the server shuts down"),
        psycopg.errors.QueryCanceled("canceling statement due to statement timeout"),
        psycopg.errors.LockNotAvailable("canceling statement due to lock timeout"),
    ],
)
def test_another_database_error_is_not_tried_again(
    monkeypatch: pytest.MonkeyPatch, driver_error: Exception
) -> None:
    """A timeout too: the request answers 503 at once, and the client sends it again."""
    engine, stopping = at_reviews(monkeypatch, database_error(driver_error), times=1)

    with pytest.raises(OperationalError):
        engine.cancel(TOP)

    assert stopping.opened == 1
    assert engine.get(TOP).status == "active"
