"""Engine edges: refusals and wording that a person reads, and guards against races."""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from stateous.control.engine import Engine, EventNotAllowed, InvalidContext
from stateous.control.graph import Graph
from stateous.control.store import Store, Transaction
from stateous.model.wire import Outcome, Schedule, TaskGrant

from ...parallel import DESIGN, SECURITY, TOP, parallel_document
from .support import TESTS_PASSED, TICKET, Clock, kinds, new_engine, sdlc


def claimed(engine: Engine, states: list[str] | None = None) -> TaskGrant:
    grant = engine.claim("box-1", states or ["triage", "coding", "testing"])
    assert grant is not None
    return grant


# ── a failed task whose `error` transition cannot move it ────────────────


def _error_needs_urgent() -> dict[str, Any]:
    """`sdlc`, but `coding` takes `error` only for an urgent ticket."""
    document = sdlc()
    for transition in document["transitions"]:
        if transition["from"] == "coding" and transition.get("on") == "error":
            transition["when"] = "ctx['urgent'] == true"
    return document


def test_a_failed_task_fails_the_token_when_its_error_transition_cannot_move_it() -> None:
    engine = new_engine(_error_needs_urgent())
    engine.start("T-1", TICKET)
    for _ in range(3):  # coding: retries=2, so three attempts
        failure = Outcome(kind="failed", error="boom")
        engine.report(claimed(engine).task, failure, supervisor="box-1")

    token = engine.get("T-1")
    assert (token.state, token.status) == ("coding", "failed")
    last = engine.history("T-1")[-1]
    assert last.kind == "failed"
    assert last.data["reason"] == (
        "'coding' failed, and 'error' cannot move it: "
        "no guard matched 'error' from 'coding', and no otherwise"
    )


# ── a timer that a routine set, and that the graph does not take ─────────


def test_a_routine_timer_whose_event_is_refused_is_recorded_and_the_token_stays() -> None:
    clock = Clock()
    engine = new_engine(clock=clock)
    engine.start("T-1", TICKET)
    grant = claimed(engine)
    timer = Schedule(event="passed", payload=TESTS_PASSED, after="10m")
    engine.report(grant.task, Outcome(kind="waiting", schedules=(timer,)), supervisor="box-1")

    clock.advance(minutes=11)
    fired = engine.tick()

    assert fired == 1
    token = engine.get("T-1")
    assert (token.state, token.status) == ("coding", "active")
    rejected = [e.data for e in engine.history("T-1") if e.kind == "event_rejected"]
    assert rejected == [{"event": "passed", "reason": "no transition for 'passed' from 'coding'"}]


# ── two START guards that hold ───────────────────────────────────────────


def _two_start_guards() -> dict[str, Any]:
    """`sdlc`, with a second START guard that holds for every ticket."""
    document = sdlc()
    document["transitions"].insert(
        1, {"from": "__start__", "to": "testing", "when": "ctx['repo'] == 'acme/api'"}
    )
    return document


def test_a_token_does_not_start_when_two_start_guards_hold() -> None:
    engine = new_engine(_two_start_guards())

    with pytest.raises(InvalidContext) as caught:
        engine.start("T-1", {**TICKET, "urgent": True})

    assert str(caught.value) == (
        "more than one transition from START holds ('triage', 'testing'); compile the "
        "workflow again, which refuses such guards"
    )


def test_a_parallel_state_is_refused_when_a_child_has_two_start_guards_that_hold() -> None:
    document = copy.deepcopy(parallel_document())
    transitions = document["workflows"]["security"]["transitions"]
    transitions.insert(0, {"from": "__start__", "to": "approved", "when": "ctx['repo'] == 'x'"})
    transitions.insert(0, {"from": "__start__", "to": "rejected", "when": "ctx['repo'] != 'y'"})
    transitions[2]["when"] = "ctx['repo'] == 'x'"
    engine = Engine(Store("sqlite://"), Graph(document), clock=Clock())
    engine.start(TOP, {"repo": "x"})

    with pytest.raises(EventNotAllowed) as caught:
        engine.fire(TOP, "done", None)

    assert "the child 'security' of 'reviews' cannot start: " in str(caught.value)
    assert "more than one transition from START holds" in str(caught.value)
    assert engine.get(TOP).state == "coding"


# ── the wording of a duration ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("lease", "span"),
    [
        (timedelta(seconds=30), "30 seconds"),
        (timedelta(seconds=1), "1 second"),
        (timedelta(minutes=1), "1 minute"),
        (timedelta(minutes=2), "2 minutes"),
    ],
)
def test_a_lost_lease_says_how_long_the_supervisor_was_silent(lease: timedelta, span: str) -> None:
    clock = Clock()
    engine = Engine(Store("sqlite://"), Graph(sdlc()), clock=clock, lease=lease)
    engine.start("T-1", TICKET)
    claimed(engine)

    clock.advance(seconds=lease.total_seconds() + 1)
    assert engine.tick() == 1

    failure = [e.data for e in engine.history("T-1") if e.kind == "task_failed"]
    assert failure[0]["error"] == f"box-1 stopped answering: no heartbeat for {span}"


def _listing(ids: list[int]) -> Callable[[Transaction], list[int]]:
    """A listing of the store that always gives `ids`."""

    def listed(self: Transaction) -> list[int]:
        return ids

    return listed


# ── races: the row changes between the list and the transaction ──────────
# `tick` lists ids first and looks again in a new transaction. The tests give the
# list an id that is no longer right.


def test_a_timer_that_was_cancelled_meanwhile_does_not_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    engine = new_engine(clock=clock)
    engine.start("T-1", TICKET)
    with engine.store.transaction(clock()) as tx:
        (timer,) = tx.pending_timers("T-1")
        timer_id = timer.id
    engine.fire("T-1", "done", None)  # the token leaves 'coding', so its timer is cancelled
    clock.advance(hours=7)
    monkeypatch.setattr(type(tx), "due_timers", _listing([timer_id]))

    assert engine.tick() == 0

    assert engine.get("T-1").state == "testing"


def test_a_timer_of_a_paused_token_does_not_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    engine = new_engine(clock=clock)
    engine.start("T-1", TICKET)
    engine.pause("T-1")
    with engine.store.transaction(clock()) as tx:
        (timer,) = tx.pending_timers("T-1")
        timer_id = timer.id
    clock.advance(hours=7)
    monkeypatch.setattr(type(tx), "due_timers", _listing([timer_id]))

    assert engine.tick() == 0

    assert (engine.get("T-1").state, engine.get("T-1").status) == ("coding", "paused")
    assert "timer_fired" not in kinds(engine, "T-1")


def test_a_task_that_finished_meanwhile_does_not_lose_its_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)
    engine.report(grant.task, Outcome(kind="event", event="done"), supervisor="box-1")
    with engine.store.transaction(engine.clock()) as tx:
        pass
    monkeypatch.setattr(type(tx), "expired_leases", _listing([grant.task]))

    assert engine.tick() == 0

    assert "task_failed" not in kinds(engine, "T-1")


def _at_reviews() -> Engine:
    engine = Engine(Store("sqlite://"), Graph(parallel_document()), clock=Clock())
    engine.start(TOP, {"repo": "acme/api"})
    engine.fire(TOP, "done", None)
    return engine


# ── the default clock, a child that ended before its parent paused ───────


def test_an_engine_without_a_clock_uses_the_time_of_the_machine() -> None:
    engine = Engine(Store("sqlite://"), Graph(sdlc()))

    engine.start("T-1", TICKET)

    assert engine.clock().tzinfo is not None
    assert engine.get("T-1").state == "coding"


def test_pause_and_resume_leave_a_child_that_ended_alone() -> None:
    engine = _at_reviews()
    engine.fire(SECURITY, "lgtm", None)
    engine.fire(SECURITY, "recorded", None)

    engine.pause(TOP)
    assert (engine.get(SECURITY).status, engine.get(DESIGN).status) == ("completed", "paused")
    engine.resume(TOP)

    assert (engine.get(SECURITY).status, engine.get(DESIGN).status) == ("completed", "active")
    assert engine.get(TOP).status == "active"


# ── a database error that is not the case race ───────────────────────────


def test_a_database_error_that_is_not_the_case_race_goes_to_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = new_engine()

    def broken(now: Any) -> Any:
        raise IntegrityError("INSERT", {}, Exception("some other constraint failed"))

    monkeypatch.setattr(engine.store, "transaction", broken)

    with pytest.raises(IntegrityError, match="some other constraint failed"):
        engine.start("T-1", TICKET)
