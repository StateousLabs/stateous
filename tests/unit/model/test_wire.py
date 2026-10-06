"""What crosses the network between the control plane and a supervisor (contract section 10)."""

import json
from typing import Any

import pytest

from stateous.model.wire import EventRecord, Outcome, Schedule, TaskGrant, WireError

GRANT = TaskGrant(
    task=7,
    token="TICKET-1",
    state="testing",
    visit=2,
    attempt=1,
    context={"repo": "acme/api"},
    memory={"dev": "session-1"},
    entered_by=EventRecord("failed", {"passed": 3, "failed": 1}),
)


def trip(data: dict[str, Any]) -> Any:
    return json.loads(json.dumps(data))


def test_a_grant_survives_the_trip() -> None:
    assert TaskGrant.from_json(trip(GRANT.to_json())) == GRANT


def test_a_grant_keys_are_its_field_names() -> None:
    assert GRANT.to_json() == {
        "task": 7,
        "token": "TICKET-1",
        "state": "testing",
        "visit": 2,
        "attempt": 1,
        "context": {"repo": "acme/api"},
        "memory": {"dev": "session-1"},
        "entered_by": {"name": "failed", "payload": {"passed": 3, "failed": 1}},
    }


def test_none_fields_are_left_out() -> None:
    first = TaskGrant(7, "T-1", "coding", 1, 1, {}, {}, entered_by=None)

    assert "entered_by" not in first.to_json()
    assert TaskGrant.from_json(trip(first.to_json())) == first
    assert EventRecord("done").to_json() == {"name": "done"}
    assert Schedule("remind", None, "1d").to_json() == {"event": "remind", "after": "1d"}


@pytest.mark.parametrize(
    "outcome",
    [
        Outcome("event", event="passed", payload={"passed": 3, "failed": 0}),
        Outcome("event", event="done"),
        Outcome("waiting", context={"pr": 88}),
        Outcome("failed", error="RuntimeError: boom"),
        Outcome(
            "waiting",
            schedules=(
                Schedule("remind", {"who": "alex"}, "1d"),
                Schedule("escalate", None, "30m"),
            ),
        ),
    ],
)
def test_every_outcome_survives_the_trip(outcome: Outcome) -> None:
    assert Outcome.from_json(trip(outcome.to_json())) == outcome


def test_an_outcome_always_carries_its_context_and_schedules() -> None:
    assert Outcome("waiting").to_json() == {"kind": "waiting", "context": {}, "schedules": []}


def test_a_minimal_outcome_is_read_with_defaults() -> None:
    assert Outcome.from_json({"kind": "waiting"}) == Outcome("waiting")


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ([], "an outcome must be a JSON object"),
        ({"kind": "exploded"}, "'kind' must be 'event', 'waiting' or 'failed'"),
        ({"kind": "event"}, "an 'event' outcome needs 'event' as a string"),
        ({"kind": "failed"}, "a 'failed' outcome needs 'error' as a string"),
        ({"kind": "waiting", "context": [1]}, "'context' must be a JSON object or null"),
        ({"kind": "waiting", "payload": "x"}, "'payload' must be a JSON object or null"),
        ({"kind": "waiting", "schedules": {"event": "x"}}, "'schedules' must be a list"),
        ({"kind": "waiting", "schedules": [{"event": "x"}]}, "a schedule needs 'after'"),
        (
            {"kind": "waiting", "schedules": [{"event": "x", "after": "soon"}]},
            "'soon' is not a duration",
        ),
        ({"kind": "waiting", "schedules": [{"event": 3, "after": "1h"}]}, "needs 'event'"),
    ],
)
def test_a_malformed_outcome_is_refused_with_a_reason(data: Any, reason: str) -> None:
    with pytest.raises(WireError, match=reason):
        Outcome.from_json(data)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"task": "7"}, "a task grant needs 'task' as a whole number"),
        ({"task": True}, "a task grant needs 'task' as a whole number"),
        ({"token": None}, "a task grant needs 'token' as a string"),
        ({"context": None}, "a task grant needs 'context' as a JSON object"),
        ({"memory": []}, "'memory' must be a JSON object or null"),
        ({"entered_by": {"payload": {}}}, "an event record needs 'name' as a string"),
    ],
)
def test_a_malformed_grant_is_refused_with_a_reason(change: dict[str, Any], reason: str) -> None:
    data = {**GRANT.to_json(), **change}

    with pytest.raises(WireError, match=reason):
        TaskGrant.from_json(data)


def test_a_wire_error_is_a_value_error() -> None:
    assert issubclass(WireError, ValueError)
