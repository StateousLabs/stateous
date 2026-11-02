"""Notices: each change that a developer's handler hears of is written in the
transaction of the change. Nothing is written while notices are off."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from stateous.control.engine import Engine, EventNotAllowed
from stateous.control.graph import Graph
from stateous.control.store import Store, tables
from stateous.model.wire import Outcome

from ...parallel import DESIGN, TOP, parallel_document
from .support import TICKET, Clock, sdlc

TESTS = {"passed": 3, "failed": 0}


def engine_with_notices(document: dict[str, Any] | None = None, *, on: bool = True) -> Engine:
    store = Store("sqlite://")
    store.migrate(notices=on)
    return Engine(store, Graph(document or sdlc()), clock=Clock())


def notices(engine: Engine) -> list[tables.Notice]:
    with engine.store.transaction(engine.clock()) as tx:
        session = tx._session  # pyright: ignore[reportPrivateUsage]
        return list(session.scalars(select(tables.Notice).order_by(tables.Notice.id)))


def shown(engine: Engine) -> list[tuple[str, str, str | None, str]]:
    return [(n.token, n.kind, n.event, n.state) for n in notices(engine)]


def report(engine: Engine, state: str, outcome: Outcome) -> None:
    grant = engine.claim("box-1", [state])
    assert grant is not None
    engine.report(grant.task, outcome, supervisor="box-1")


def test_no_notice_rows_when_notices_are_off() -> None:
    engine = engine_with_notices(on=False)
    engine.start("T-1", TICKET)
    report(engine, "coding", Outcome(kind="event", event="stuck"))

    assert engine.get("T-1").status == "completed"
    assert notices(engine) == []


def test_an_accepted_event_writes_one_notice_with_its_payload() -> None:
    engine = engine_with_notices()
    engine.start("T-1", TICKET)
    report(engine, "coding", Outcome(kind="event", event="done"))
    report(engine, "testing", Outcome(kind="event", event="passed", payload=TESTS))

    rows = notices(engine)
    assert shown(engine) == [
        ("T-1", "event", "done", "testing"),
        ("T-1", "event", "passed", "__end__"),
        ("T-1", "completed", None, "testing"),
    ]
    assert rows[1].payload == TESTS
    assert rows[1].workflow == "sdlc"
    assert rows[1].family == "T-1"
    assert rows[1].parent is None


def test_a_rejected_event_writes_no_notice() -> None:
    engine = engine_with_notices()
    engine.start("T-1", TICKET)

    with pytest.raises(EventNotAllowed):
        engine.fire("T-1", "passed", TESTS)

    assert notices(engine) == []


def test_a_replayed_event_writes_a_notice_on_resume() -> None:
    engine = engine_with_notices()
    engine.start("T-1", TICKET)
    engine.pause("T-1")
    engine.fire("T-1", "stuck", None)
    assert notices(engine) == []

    engine.resume("T-1")

    assert shown(engine) == [
        ("T-1", "event", "stuck", "__end__"),
        ("T-1", "completed", None, "coding"),
    ]


def test_end_fail_and_cancel_write_their_notices() -> None:
    engine = engine_with_notices()
    engine.start("T-1", TICKET)
    engine.start("T-2", TICKET)
    engine.cancel("T-1")
    report(engine, "coding", Outcome(kind="event", event="done"))  # T-2, to testing
    report(engine, "testing", Outcome(kind="failed", error="the runner broke"))

    assert ("T-1", "cancelled", None, "coding") in shown(engine)
    assert [kind for token, kind, _, _ in shown(engine) if token == "T-2"] == [
        "event",
        "task_failed",
        "failed",
    ]


def test_a_task_that_retries_writes_no_notice_and_its_last_failure_does() -> None:
    engine = engine_with_notices()
    engine.start("T-1", TICKET)
    failed = Outcome(kind="failed", error="flaky")

    report(engine, "coding", failed)
    report(engine, "coding", failed)
    assert notices(engine) == []

    report(engine, "coding", failed)  # the third attempt: coding has 2 retries

    assert shown(engine) == [
        ("T-1", "task_failed", None, "coding"),
        ("T-1", "event", "error", "triage"),
    ]


def test_a_child_token_writes_notices_with_its_parent() -> None:
    engine = engine_with_notices(parallel_document("all"))
    engine.start(TOP, {"repo": "acme/api"})
    engine.fire(TOP, "done", None)
    engine.fire(DESIGN, "lgtm", None)

    row = next(n for n in notices(engine) if n.token == DESIGN)
    assert (row.kind, row.event, row.parent, row.family, row.workflow) == (
        "event",
        "lgtm",
        TOP,
        TOP,
        "design",
    )


def test_a_notice_is_in_the_same_transaction() -> None:
    engine = engine_with_notices()
    engine.start("T-1", TICKET)
    with (
        pytest.raises(RuntimeError, match="stop here"),
        engine.store.transaction(engine.clock()) as tx,
    ):
        row = tx.token("T-1")
        assert row is not None
        tx.append("T-1", "note", text="a change")
        tx.notify(row, "event", workflow="sdlc", event="stuck")
        raise RuntimeError("stop here")

    assert notices(engine) == []
    assert [entry.kind for entry in engine.history("T-1")][-1] != "note"
