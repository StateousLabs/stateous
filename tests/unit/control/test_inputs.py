"""What the engine refuses before it writes: values the store cannot hold, and the
errors it raises for them."""

import math

import pytest

from stateous.control import engine as engine_module
from stateous.control.engine import (
    AttemptOver,
    ChildToken,
    EngineError,
    EventNotAllowed,
    InvalidArgument,
    InvalidContext,
    InvalidPayload,
    InvalidTokenId,
    TaskNotFound,
    TaskNotHeld,
    TokenNotActive,
    TokenNotFound,
)
from stateous.model.wire import Outcome, Schedule, TaskGrant

from .support import TESTS_FAILED, TICKET, kinds, new_engine

# ── one base for the engine's errors ────────────────────────────────────


def test_every_engine_error_is_an_engine_error_and_keeps_its_base() -> None:
    expected: dict[type[Exception], type[Exception]] = {
        TokenNotFound: LookupError,
        TaskNotFound: LookupError,
        InvalidContext: ValueError,
        InvalidPayload: ValueError,
        InvalidTokenId: ValueError,
        EventNotAllowed: ValueError,
        TokenNotActive: ValueError,
        TaskNotHeld: ValueError,
        AttemptOver: TaskNotHeld,
        InvalidArgument: ValueError,
        ChildToken: ValueError,
    }
    for error, base in expected.items():
        assert issubclass(error, EngineError), error
        assert issubclass(error, base), error


def test_the_test_above_names_every_error_of_the_engine() -> None:
    errors = {
        value
        for value in vars(engine_module).values()
        if isinstance(value, type)
        and issubclass(value, Exception)
        and value.__module__ == engine_module.__name__
        and value is not EngineError
    }
    assert errors == {
        TokenNotFound, TaskNotFound, InvalidContext, InvalidPayload, InvalidTokenId,
        EventNotAllowed, TokenNotActive, TaskNotHeld, AttemptOver, InvalidArgument, ChildToken,
    }  # fmt: skip


# ── NaN and Infinity are not JSON ───────────────────────────────────────


def claimed_in_testing() -> tuple[engine_module.Engine, TaskGrant]:
    engine = new_engine()
    engine.start("T-1", TICKET)
    engine.fire("T-1", "done", None)
    grant = engine.claim("box-1", ["testing"])
    assert grant is not None
    return engine, grant


@pytest.mark.parametrize("number", [math.nan, math.inf, -math.inf])
def test_a_starting_context_with_nan_or_infinity_is_refused(number: float) -> None:
    engine = new_engine()

    with pytest.raises(InvalidContext, match="not JSON"):
        engine.start("T-1", {**TICKET, "score": number})

    assert engine.tokens() == []


@pytest.mark.parametrize("number", [math.nan, math.inf])
def test_a_fired_payload_with_nan_or_infinity_is_refused(number: float) -> None:
    engine, _ = claimed_in_testing()

    with pytest.raises(InvalidPayload, match="not JSON"):
        engine.fire("T-1", "passed", {**TESTS_FAILED, "ratio": number})

    assert kinds(engine, "T-1")[-1] == "task_claimed"


def test_context_changes_with_nan_are_refused() -> None:
    engine, grant = claimed_in_testing()
    outcome = Outcome(kind="waiting", context={"score": math.nan})

    with pytest.raises(InvalidContext, match="not JSON"):
        engine.report(grant.task, outcome, supervisor="box-1")

    assert "score" not in engine.get("T-1").context


def test_a_reported_payload_with_infinity_is_refused() -> None:
    engine, grant = claimed_in_testing()
    outcome = Outcome(kind="event", event="passed", payload={**TESTS_FAILED, "ratio": math.inf})

    with pytest.raises(InvalidPayload, match="not JSON"):
        engine.report(grant.task, outcome, supervisor="box-1")

    assert engine.heartbeat("box-1", [grant.task]) == []  # still held


def test_a_schedule_payload_with_nan_is_refused() -> None:
    engine, grant = claimed_in_testing()
    timer = Schedule(event="passed", payload={**TESTS_FAILED, "ratio": math.nan}, after="10m")

    with pytest.raises(InvalidPayload, match="not JSON"):
        engine.report(grant.task, Outcome(kind="waiting", schedules=(timer,)), supervisor="box-1")

    assert kinds(engine, "T-1")[-1] == "task_claimed"  # nothing recorded


# ── what the columns can hold ───────────────────────────────────────────


@pytest.mark.parametrize("supervisor", ["", "x" * 257], ids=["empty", "257"])
def test_a_supervisor_name_is_1_to_256_characters(supervisor: str) -> None:
    engine, grant = claimed_in_testing()

    with pytest.raises(InvalidArgument, match="1 to 256 characters"):
        engine.claim(supervisor, ["testing"])
    with pytest.raises(InvalidArgument, match="1 to 256 characters"):
        engine.heartbeat(supervisor, [grant.task])
    with pytest.raises(InvalidArgument, match="1 to 256 characters"):
        engine.report(grant.task, Outcome(kind="waiting"), supervisor=supervisor)


def test_a_supervisor_name_of_256_characters_is_taken() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)

    assert engine.claim("x" * 256, ["coding"]) is not None


@pytest.mark.parametrize("limit", [0, -1, 1001])
def test_a_token_listing_takes_1_to_1000_tokens(limit: int) -> None:
    engine = new_engine()

    with pytest.raises(InvalidArgument, match="1 to 1000"):
        engine.tokens(limit=limit)


def test_a_token_listing_of_1000_is_taken() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)

    assert len(engine.tokens(limit=1000)) == 1
