"""Purge: a token that ended, and its children, are deleted for good, from every table,
in one transaction (see the docs, Concepts > Pause, resume and cancel).

The tests open `Store("sqlite://")`, so with STATEOUS_TEST_POSTGRES set they run on
Postgres too (tests/conftest.py)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select

from stateous.control.engine import Engine, Purged
from stateous.control.errors import ChildToken, TaskNotFound, TokenLive
from stateous.control.graph import Graph
from stateous.control.store import Store, tables
from stateous.model.wire import Outcome, Schedule

from ...parallel import DESIGN, SECURITY, TOP, parallel_document
from .support import TICKET, Clock, kinds, new_engine

OTHER = "OTHER-2"
"""A token that a purge of `TOP` must leave as it is. Its id does not contain `TOP`."""


def engine_with_notices(document: dict[str, Any] | None = None) -> Engine:
    store = Store("sqlite://")
    store.migrate(notices=True)
    return Engine(store, Graph(document or parallel_document()), clock=Clock())


def every_row(engine: Engine) -> dict[str, list[dict[str, Any]]]:
    """Each table of the engine, with all of its rows."""
    found: dict[str, list[dict[str, Any]]] = {}
    with engine.store.engine.connect() as connection:
        for table in tables.Base.metadata.sorted_tables:
            rows = connection.execute(select(table)).mappings().all()
            found[table.name] = [dict(row) for row in rows]
    return found


def rows_naming(engine: Engine, text: str) -> list[tuple[str, dict[str, Any]]]:
    """Each row, of any table, that has `text` in any of its values."""
    return [
        (table, row)
        for table, rows in every_row(engine).items()
        for row in rows
        if text in json.dumps(row, default=str)
    ]


def task_ids(engine: Engine) -> dict[int, str]:
    return {row["id"]: row["token"] for row in every_row(engine)["tasks"]}


def a_family_that_did_everything(engine: Engine) -> None:
    """`TOP`, with labels and a start key, in `reviews` with both children. The security
    child's routine wrote a note and a memory key, and waits on a timer; its claim is
    the supervisor's last claim. Then `TOP` is cancelled, and its children with it."""
    engine.start(TOP, TICKET | {"branch": "b"}, labels={"user": "u1"}, start_key=("", "k-1"))
    engine.fire(TOP, "done", None)
    grant = engine.claim("box", ["security/triage"], request="r-1")
    assert grant is not None
    engine.note(grant.task, "free text about the customer")
    engine.set_memory(grant.task, "draft", {"text": "private"})
    timer = Schedule(event="lgtm", payload=None, after="1d")
    engine.report(grant.task, Outcome(kind="waiting", schedules=(timer,)), supervisor="box")
    engine.cancel(TOP)


def another_token(engine: Engine) -> None:
    engine.start(OTHER, TICKET, labels={"user": "u2"}, start_key=("", "k-2"))
    engine.fire(OTHER, "done", None)


def test_a_purge_leaves_no_row_that_names_the_token_or_its_children() -> None:
    engine = engine_with_notices()
    a_family_that_did_everything(engine)
    another_token(engine)
    named = rows_naming(engine, TOP)
    assert {table for table, _ in named} >= {
        "tokens", "ledger", "tasks", "timers", "token_labels", "notices", "start_keys",
    }  # fmt: skip
    family_tasks = {task for task, token in task_ids(engine).items() if token.startswith(TOP)}
    others_before = rows_naming(engine, OTHER)

    purged = engine.purge(TOP)

    assert purged == Purged(token=TOP, tokens=3)
    assert rows_naming(engine, TOP) == []
    claims = every_row(engine)["claims"]
    assert all(claim["task_id"] not in family_tasks for claim in claims)
    assert rows_naming(engine, OTHER) == others_before


def test_a_purge_deletes_the_children_of_every_visit() -> None:
    engine = Engine(Store("sqlite://"), Graph(parallel_document()), clock=Clock())
    engine.start(TOP, TICKET)
    engine.fire(TOP, "done", None)
    for child in (SECURITY, DESIGN):  # both reject: T-1 codes again
        state = child.rsplit(".", 1)[1] + "/triage"
        grant = engine.claim("box", [state])
        assert grant is not None
        engine.report(
            grant.task, Outcome(kind="event", event="deny", payload={"count": 1}), supervisor="box"
        )
        grant = engine.claim("box", [child.rsplit(".", 1)[1] + "/rejected"])
        assert grant is not None
        engine.report(grant.task, Outcome(kind="event", event="recorded"), supervisor="box")
    assert engine.get(TOP).state == "coding"
    engine.fire(TOP, "done", None)  # a second visit of `reviews`: two more children
    engine.cancel(TOP)

    assert engine.purge(TOP) == Purged(token=TOP, tokens=5)
    assert rows_naming(engine, TOP) == []


def test_an_ended_token_of_each_status_can_be_purged() -> None:
    engine = new_engine()
    engine.start("DONE-1", TICKET)
    engine.fire("DONE-1", "stuck", None)
    engine.start("FAILED-1", TICKET)
    engine.fire("FAILED-1", "done", None)
    grant = engine.claim("box", ["testing"])
    assert grant is not None  # `testing` has no retries and no `error`: a failure fails
    engine.report(grant.task, Outcome(kind="failed", error="boom"), supervisor="box")
    engine.start("CANCELLED-1", TICKET)
    engine.cancel("CANCELLED-1")
    statuses = {t: engine.get(t).status for t in ("DONE-1", "FAILED-1", "CANCELLED-1")}
    assert statuses == {"DONE-1": "completed", "FAILED-1": "failed", "CANCELLED-1": "cancelled"}

    for token in statuses:
        assert engine.purge(token) == Purged(token=token, tokens=1)
        assert rows_naming(engine, token) == []


@pytest.mark.parametrize("paused", [False, True])
def test_a_live_token_is_refused_and_nothing_is_deleted(paused: bool) -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    if paused:
        engine.pause("T-1")
    before = every_row(engine)

    with pytest.raises(TokenLive) as refused:
        engine.purge("T-1")

    assert refused.value.code == "token_live"
    assert "cancel" in str(refused.value)
    assert every_row(engine) == before


def test_a_parent_whose_child_is_live_is_refused() -> None:
    engine = Engine(Store("sqlite://"), Graph(parallel_document()), clock=Clock())
    engine.start(TOP, TICKET)
    engine.fire(TOP, "done", None)

    with pytest.raises(TokenLive):
        engine.purge(TOP)

    assert engine.get(SECURITY).status == "active"


def test_a_child_cannot_be_purged_on_its_own() -> None:
    engine = Engine(Store("sqlite://"), Graph(parallel_document()), clock=Clock())
    engine.start(TOP, TICKET)
    engine.fire(TOP, "done", None)
    engine.cancel(TOP)

    with pytest.raises(ChildToken, match=f"purge {TOP!r} instead"):
        engine.purge(SECURITY)

    assert engine.get(SECURITY).status == "cancelled"


def test_an_unknown_token_is_purged_as_nothing_so_a_purge_can_be_called_again() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    engine.cancel("T-1")

    assert engine.purge("NOPE") == Purged(token="NOPE", tokens=0)
    assert engine.purge("T-1") == Purged(token="T-1", tokens=1)
    assert engine.purge("T-1") == Purged(token="T-1", tokens=0)


def test_a_purged_id_can_start_a_new_token_with_a_new_history() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    engine.cancel("T-1")
    engine.purge("T-1")

    engine.start("T-1", TICKET)

    assert kinds(engine, "T-1")[0] == "started"
    assert engine.history("T-1")[0].seq == 1
    assert engine.get("T-1").status == "active"


def test_a_new_task_never_gets_the_id_of_a_purged_task() -> None:
    """A supervisor can still run a routine of the purged task. Its late report must
    not reach a new task that has the same id."""
    engine = new_engine()
    engine.start("T-1", TICKET)
    engine.cancel("T-1")
    purged = set(task_ids(engine))
    engine.purge("T-1")

    engine.start("T-2", TICKET)

    assert set(task_ids(engine)).isdisjoint(purged)


def test_a_late_report_of_a_purged_task_is_task_not_found() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = engine.claim("box", ["coding"])
    assert grant is not None
    engine.cancel("T-1")
    engine.purge("T-1")

    with pytest.raises(TaskNotFound):
        engine.report(grant.task, Outcome(kind="event", event="done"), supervisor="box")
    assert engine.heartbeat("box", [grant.task]) == [grant.task]
