"""Idempotent claims: a claim that names a request id, and its repeat after a lost answer."""

import pytest

from stateous.control.engine import Engine, InvalidArgument
from stateous.model.wire import Outcome, TaskGrant

from .support import TICKET, Clock, kinds, new_engine

STATES = ["triage", "coding", "testing"]


def claimed(engine: Engine, request: str | None, supervisor: str = "box-1") -> TaskGrant:
    grant = engine.claim(supervisor, STATES, request=request)
    assert grant is not None
    return grant


def lease_of(engine: Engine, task: int) -> object:
    with engine.store.transaction(engine.clock()) as tx:
        row = tx.task(task)
        assert row is not None
        return row.lease_until


def test_a_repeated_claim_gets_the_same_grant_and_claims_no_second_task() -> None:
    clock = Clock()
    engine = new_engine(clock=clock)
    engine.start("T-1", TICKET)
    engine.start("T-2", TICKET)
    first = claimed(engine, "r-1")
    clock.advance(seconds=90)

    again = claimed(engine, "r-1")

    assert again == first
    assert kinds(engine, "T-1").count("task_claimed") == 1
    assert "task_claimed" not in kinds(engine, "T-2")
    assert lease_of(engine, first.task) == (clock.now + engine.lease).replace(tzinfo=None)


def test_a_new_request_id_claims_a_new_task() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    engine.start("T-2", TICKET)
    first = claimed(engine, "r-1")

    second = claimed(engine, "r-2")

    assert (first.token, second.token) == ("T-1", "T-2")


def test_a_repeat_after_the_task_was_reported_is_a_new_claim() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    first = claimed(engine, "r-1")
    engine.report(first.task, Outcome(kind="event", event="done"), supervisor="box-1")

    again = claimed(engine, "r-1")

    assert (again.state, again.task) == ("testing", first.task + 1)


def test_a_repeat_after_the_lease_ended_is_a_new_claim() -> None:
    clock = Clock()
    engine = new_engine(clock=clock)
    engine.start("T-1", TICKET)
    first = claimed(engine, "r-1")
    clock.advance(minutes=3)  # past the 2-minute lease; the clock has not yet run

    assert engine.claim("box-1", STATES, request="r-1") is None
    engine.tick()  # the lease ends: attempt 1 failed, and the task is queued again
    again = claimed(engine, "r-1")

    assert (again.task, again.attempt) == (first.task, 2)


def test_a_repeat_after_no_task_claims_normally() -> None:
    engine = new_engine()
    assert engine.claim("box-1", STATES, request="r-1") is None
    engine.start("T-1", TICKET)

    grant = claimed(engine, "r-1")

    assert grant.token == "T-1"


def test_the_same_request_id_of_another_supervisor_is_its_own_claim() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    engine.start("T-2", TICKET)
    first = claimed(engine, "r-1", "box-1")

    second = claimed(engine, "r-1", "box-2")

    assert (first.token, second.token) == ("T-1", "T-2")


def test_a_claim_without_a_request_id_claims_a_new_task_each_time() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    engine.start("T-2", TICKET)
    first = claimed(engine, "r-1")

    second = claimed(engine, None)
    third = engine.claim("box-1", STATES)

    assert (first.token, second.token, third) == ("T-1", "T-2", None)


def test_a_claim_without_a_request_id_keeps_the_remembered_one() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    engine.start("T-2", TICKET)
    first = claimed(engine, "r-1")
    claimed(engine, None)

    assert claimed(engine, "r-1") == first


@pytest.mark.parametrize("request_id", ["", "x" * 65, "a b", "ü", "a/b"])
def test_a_request_id_is_1_to_64_letters_digits_dots_underscores_or_dashes(
    request_id: str,
) -> None:
    engine = new_engine()

    with pytest.raises(InvalidArgument, match="request id"):
        engine.claim("box-1", STATES, request=request_id)


def test_the_longest_request_id_is_taken() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)

    assert engine.claim("box-1", STATES, request="A9._-" + "x" * 59) is not None
