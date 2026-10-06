"""Many writers at once, on a database that lets them: Postgres.

SQLite takes one transaction at a time, so these can only fail on Postgres.
"""

import threading
from collections.abc import Callable, Iterator
from contextlib import suppress
from typing import Any

import pytest

from stateous.control.engine import Engine, InvalidTokenId, TaskNotHeld, Token
from stateous.control.graph import Graph
from stateous.control.store import Store
from stateous.model.wire import Outcome

from ..conftest import fresh_postgres_database, needs_postgres
from ..unit.control.support import TICKET, sdlc

pytestmark = needs_postgres


def pinged() -> dict[str, Any]:
    """`sdlc()`, with `ping`: an event that brings `coding` back to itself."""
    document = sdlc()
    document["events"]["ping"] = {}
    document["states"]["coding"]["emits"].append("ping")
    document["transitions"].append({"from": "coding", "to": "coding", "on": "ping"})
    return document


@pytest.fixture
def engine() -> Iterator[Engine]:
    store = Store(fresh_postgres_database())
    yield Engine(store, Graph(pinged()))
    store.close()


def at_once(count: int, work: Callable[[int], Any]) -> list[Any]:
    """Run `work(i)` in `count` threads that start together. Returns results or errors."""
    barrier = threading.Barrier(count)
    results: list[Any] = [None] * count

    def run(i: int) -> None:
        barrier.wait()
        try:
            results[i] = work(i)
        except Exception as error:
            results[i] = error

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


def test_events_fired_at_one_token_at_once_are_all_recorded_in_order(engine: Engine) -> None:
    engine.start("T-1", TICKET)

    results = at_once(8, lambda i: [engine.fire("T-1", "ping", None) for _ in range(5)])

    assert [r for r in results if isinstance(r, Exception)] == []
    seqs = [entry.seq for entry in engine.history("T-1")]
    assert seqs == list(range(1, len(seqs) + 1))
    assert engine.get("T-1").visit == 41  # entered once, then 40 pings


def test_every_task_is_claimed_by_exactly_one_supervisor(engine: Engine) -> None:
    for n in range(30):
        engine.start(f"T-{n}", TICKET)

    def claim_all(i: int) -> list[int]:
        taken: list[int] = []
        while (grant := engine.claim(f"box-{i}", ["coding"])) is not None:
            taken.append(grant.task)
        return taken

    results = at_once(6, claim_all)

    assert [r for r in results if isinstance(r, Exception)] == []
    claimed = [task for taken in results for task in taken]
    assert len(claimed) == 30
    assert len(set(claimed)) == 30


def test_claiming_and_moving_the_same_tokens_at_once_never_deadlocks(engine: Engine) -> None:
    tokens = [f"T-{n}" for n in range(20)]
    for token in tokens:
        engine.start(token, TICKET)

    def work(i: int) -> None:
        for _ in range(10):
            if i % 2:
                grant = engine.claim(f"box-{i}", ["coding"])
                if grant is not None:
                    ping = Outcome(kind="event", event="ping")
                    with suppress(TaskNotHeld):  # a fired ping can end the task first
                        engine.report(grant.task, ping, supervisor=f"box-{i}")
            else:
                for token in tokens:
                    engine.fire(token, "ping", None)

    results = at_once(6, work)

    assert [r for r in results if isinstance(r, Exception)] == []


def test_heartbeats_and_moves_at_once_never_deadlock(engine: Engine) -> None:
    tokens = [f"T-{n}" for n in range(10)]
    for token in tokens:
        engine.start(token, TICKET)
    held: list[int] = []
    while (grant := engine.claim("box-1", ["coding"])) is not None:
        held.append(grant.task)

    def work(i: int) -> None:
        for _ in range(10):
            if i == 0:  # one supervisor sends one heartbeat at a time
                engine.heartbeat("box-1", held)
            else:
                for token in tokens:
                    engine.fire(token, "ping", None)

    results = at_once(3, work)

    assert [r for r in results if isinstance(r, Exception)] == []
    assert engine.heartbeat("box-1", held) == sorted(held)  # the pings ended them all


def test_token_ids_differing_only_in_case_started_at_once_make_one_token(engine: Engine) -> None:
    ids = ["T-1", "t-1", "T-1", "t-1", "T-1", "t-1", "T-1", "t-1"]

    results = at_once(len(ids), lambda i: engine.start(ids[i], TICKET))

    assert all(isinstance(r, Token | InvalidTokenId) for r in results), results
    assert len({r.token for r in results if isinstance(r, Token)}) == 1
    assert len(engine.tokens()) == 1
