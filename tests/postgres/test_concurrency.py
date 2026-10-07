"""Many writers at once, on a database that lets them: Postgres.

SQLite takes one transaction at a time, so these can only fail on Postgres.
"""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from stateous.control.engine import Engine, InvalidTokenId, TaskNotHeld, Token, child_id
from stateous.control.graph import Graph
from stateous.control.store import Store
from stateous.model.wire import Outcome

from ..conftest import fresh_postgres_database, needs_postgres
from ..parallel import parallel_document
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


def test_a_first_claim_and_its_repeat_at_once_get_the_same_grant(engine: Engine) -> None:
    for n in range(40):
        engine.start(f"T-{n}", TICKET)

    for round_ in range(10):  # each round is the first claim of a new supervisor
        name = f"box-{round_}"

        results = at_once(2, lambda i, name=name: engine.claim(name, ["coding"], request="r-1"))

        assert [r for r in results if isinstance(r, Exception)] == []
        assert results[0] is not None
        assert results[0] == results[1]


# ── a family: a parent and its children (design section 8) ──────────────


def parallel_engine() -> tuple[Engine, Store]:
    store = Store(fresh_postgres_database())
    return Engine(store, Graph(parallel_document())), store


def test_children_that_end_at_once_join_their_parent_once() -> None:
    engine, store = parallel_engine()
    try:
        for n in range(4):
            engine.start(f"T-{n}", TICKET)
            engine.fire(f"T-{n}", "done", None)

        def finish(i: int) -> None:
            child = child_id(f"T-{i // 2}", "reviews", 1, ("security", "design")[i % 2])
            engine.fire(child, "lgtm", None)
            engine.fire(child, "recorded", None)

        results = at_once(8, finish)

        assert [r for r in results if isinstance(r, Exception)] == []
        for n in range(4):
            assert engine.get(f"T-{n}").state == "merging"
            entries = engine.history(f"T-{n}")
            accepted = [e.data["event"] for e in entries if e.kind == "event_accepted"]
            assert accepted.count("joined") == 1
    finally:
        store.close()


def test_a_childs_event_and_its_parents_pause_at_once_take_turns() -> None:
    engine, store = parallel_engine()
    try:
        engine.start("T-1", TICKET)
        engine.fire("T-1", "done", None)
        security = child_id("T-1", "reviews", 1, "security")

        def act(i: int) -> Token:
            return engine.pause("T-1") if i == 0 else engine.fire(security, "lgtm", None).token

        results = at_once(2, act)

        assert [r for r in results if isinstance(r, Exception)] == []
        assert (engine.get("T-1").status, engine.get(security).status) == ("paused", "paused")
    finally:
        store.close()


def test_a_cascade_that_the_database_stops_for_a_deadlock_runs_once_more(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A heartbeat locks task rows in the order of their ids. A cancel locks the tasks
    of the children in the order of the workflows: here, the other order. A second
    connection plays the heartbeat, one row at a time, so the two meet in a circle.
    Postgres stops one of them. It is the cancel when its deadlock check runs first;
    under a heavy load it can be the heartbeat, so the test tries up to three times."""
    engine, store = parallel_engine()
    try:
        stopped = [deadlock(engine, store, f"T-{n}") for n in range(3)]
    finally:
        store.close()

    assert stopped[0] or stopped[1] or stopped[2], "Postgres never stopped the cancel"
    lines = [r.getMessage() for r in caplog.records if r.name == "stateous.engine"]
    assert lines == ["the database stopped cancel for a deadlock; trying once more"] * sum(stopped)


def deadlock(engine: Engine, store: Store, top: str) -> bool:
    """Cancel `top` while a second connection renews its children's leases in the other
    order. The cancel always ends the family. True: Postgres stopped the cancel, and
    the cancel ran once more. False: Postgres stopped the second connection."""
    engine.start(top, TICKET)
    engine.fire(top, "done", None)
    engine.fire(child_id(top, "reviews", 1, "security"), "lgtm", None)  # a larger task id
    late = engine.claim("box", ["security/approved"])
    early = engine.claim("box", ["design/triage"])
    assert late is not None and early is not None and early.task < late.task
    results: list[Any] = []

    def cancel() -> None:
        try:
            results.append(engine.cancel(top))
        except Exception as error:
            results.append(error)

    renew = text("UPDATE tasks SET lease_until = lease_until WHERE id = :id")
    heartbeat_stopped = False
    with store.engine.connect() as heartbeat:
        heartbeat.execute(renew, {"id": early.task})
        thread = threading.Thread(target=cancel)
        thread.start()
        waiting(store, until=1)  # the cancel holds the late task, waits for the early one
        try:
            heartbeat.execute(renew, {"id": late.task})  # Postgres stops one of the two
            heartbeat.commit()
        except OperationalError:
            heartbeat_stopped = True
            heartbeat.rollback()
    thread.join(timeout=30)

    assert [type(r).__name__ for r in results] == ["Token"], results
    assert engine.get(top).status == "cancelled"
    return not heartbeat_stopped


def waiting(store: Store, *, until: int) -> None:
    """Wait (at most 10 s) until `until` sessions of the store's database wait for a lock."""
    query = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
    )
    deadline = time.monotonic() + 10
    # Autocommit: in one transaction, Postgres shows the same snapshot of
    # pg_stat_activity each time.
    with store.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as watcher:
        while (watcher.scalar(query) or 0) < until:
            assert time.monotonic() < deadline, "the cancel never waited for the lock"
            time.sleep(0.02)
