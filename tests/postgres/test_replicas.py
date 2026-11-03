"""Replicas: several control planes of one workflow on one Postgres namespace."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from stateous import ControlPlane, DatabaseHeld, GraphConflict
from stateous.control.graph import Graph
from stateous.control.store.store import database_url
from stateous.model.wire import Outcome, Schedule

from ..conftest import fresh_postgres_database, needs_postgres
from ..unit.control.support import TICKET, Clock, sdlc

pytestmark = needs_postgres


def other_structure() -> dict[str, Any]:
    document = sdlc()
    document["states"]["review"] = {"emits": ["approved"]}
    document["events"]["approved"] = {}
    document["transitions"].append({"from": "review", "to": "__end__", "on": "approved"})
    document["transitions"][3] = {"from": "coding", "to": "review", "on": "done"}
    return document


def replica(url: str, document: dict[str, Any] | None = None, **options: Any) -> ControlPlane:
    return ControlPlane(
        document or sdlc(), database=url, namespace="flow", migrate=True, exclusive=False, **options
    )


def test_two_replicas_serve_one_namespace() -> None:
    url = fresh_postgres_database()
    first, second = replica(url), replica(url)
    try:
        first.start("T-1", TICKET)
        grant = second.claim("box-1", ["coding"])
        assert grant is not None and grant.token == "T-1"
    finally:
        first.close()
        second.close()


def test_two_replicas_fire_a_timer_once_and_end_a_lease_once() -> None:
    url = fresh_postgres_database()
    clock = Clock()
    first, second = replica(url, now=clock), replica(url, now=clock)
    try:
        first.start("T-1", TICKET)
        first.start("T-2", TICKET)
        timer = first.claim("box-1", ["coding"])
        leased = first.claim("box-2", ["coding"])
        assert timer is not None and leased is not None
        waiting = Outcome(
            kind="waiting", schedules=(Schedule(event="stuck", payload=None, after="10m"),)
        )
        first.report(timer.task, waiting, supervisor="box-1")
        clock.advance(minutes=11)  # the timer is due, and box-2's lease has ended

        def tick_often(plane: ControlPlane) -> None:
            for _ in range(5):
                plane.tick()

        threads = [threading.Thread(target=tick_often, args=(p,)) for p in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        kinds_1 = [entry.kind for entry in first.history(timer.token)]
        kinds_2 = [entry.kind for entry in first.history(leased.token)]
        assert kinds_1.count("timer_fired") == 1
        assert kinds_2.count("task_failed") == 1
    finally:
        first.close()
        second.close()


def test_a_replica_of_another_structure_is_refused_while_one_serves() -> None:
    url = fresh_postgres_database()
    first = replica(url)
    try:
        with pytest.raises(GraphConflict, match="replica"):
            replica(url, other_structure())
    finally:
        first.close()


def test_a_new_checksum_opens_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    url = fresh_postgres_database()
    first = replica(url)
    changed = sdlc()
    changed["states"]["coding"]["timeout"] = "2h"
    caplog.set_level(logging.WARNING)
    try:
        second = replica(url, changed)
        second.close()
    finally:
        first.close()

    assert "checksum" in caplog.text


def test_a_lapsed_replica_row_does_not_block_a_new_structure() -> None:
    url = fresh_postgres_database()
    replica(url).close()
    engine = create_engine(database_url(url))
    long_ago = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO flow.replicas (holder, structure, checksum, scope, seen) "
                "VALUES ('gone:1:x', 'old', 'old', '[]', :seen)"
            ),
            {"seen": long_ago},
        )
    engine.dispose()

    replica(url, other_structure()).close()


def test_an_exclusive_plane_and_replicas_do_not_mix() -> None:
    url = fresh_postgres_database()
    first = replica(url)
    try:
        with pytest.raises(DatabaseHeld):
            ControlPlane(sdlc(), database=url, namespace="flow", migrate=True)
    finally:
        first.close()

    exclusive = ControlPlane(sdlc(), database=url, namespace="flow", migrate=True)
    try:
        with pytest.raises(DatabaseHeld):
            replica(url)
    finally:
        exclusive.close()


def test_the_replica_row_goes_on_close() -> None:
    url = fresh_postgres_database()
    plane = replica(url)
    holder: Callable[[], int] = lambda: _rows(url)  # noqa: E731
    assert holder() == 1
    plane.close()
    assert holder() == 0
    assert Graph(sdlc()).structure  # the graph of the test is valid


def _rows(url: str) -> int:
    engine = create_engine(database_url(url))
    try:
        with engine.connect() as connection:
            return int(connection.scalar(text("SELECT count(*) FROM flow.replicas")) or 0)
    finally:
        engine.dispose()


def test_an_exclusive_plane_is_refused_by_a_replicas_lock_even_without_its_row() -> None:
    url = fresh_postgres_database()
    first = replica(url)
    engine = create_engine(database_url(url))
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM flow.replicas"))  # as if the row lapsed
    engine.dispose()
    try:
        with pytest.raises(DatabaseHeld):
            ControlPlane(sdlc(), database=url, namespace="flow", migrate=True)
    finally:
        first.close()


def test_a_replica_needs_the_psycopg_3_driver() -> None:
    pytest.importorskip("psycopg2")
    url = fresh_postgres_database().replace("postgresql://", "postgresql+psycopg2://")
    with pytest.raises(ValueError, match="psycopg 3"):
        ControlPlane(sdlc(), database=create_engine(url), namespace="flow", exclusive=False)


def test_replicas_with_different_scopes_refuse_each_other() -> None:
    from stateous import ScopeConflict

    url = fresh_postgres_database()
    first = replica(url, scope=["user"])
    try:
        with pytest.raises(ScopeConflict):
            replica(url, scope=["tenant"])
    finally:
        first.close()


def test_concurrent_starts_with_one_key_give_one_token_and_no_error() -> None:
    url = fresh_postgres_database()
    plane = ControlPlane(sdlc(), database=url, namespace="flow", migrate=True)
    barrier = threading.Barrier(4)
    made: list[str] = []
    errors: list[BaseException] = []

    def start() -> None:
        barrier.wait()
        try:
            made.append(plane.start(None, TICKET, key="order-1").token)
        except BaseException as error:  # collected and checked below
            errors.append(error)

    threads = [threading.Thread(target=start) for _ in range(4)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
    finally:
        plane.close()

    assert errors == []
    assert len(set(made)) == 1 and len(made) == 4
