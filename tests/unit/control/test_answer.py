"""`answer`: the control plane runs one call of a transport and gives its reply."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from stateous import ControlPlane
from stateous.control.answer import answer
from stateous.model.wire import Outcome, TaskGrant
from stateous.transport import Call

from .support import TICKET, sdlc, sqlite_url


@pytest.fixture
def plane(tmp_path: Path) -> Iterator[ControlPlane]:
    with ControlPlane(sdlc(), database=sqlite_url(tmp_path / "a.db"), migrate=True) as plane:
        yield plane


def claim(plane: ControlPlane) -> dict[str, Any]:
    reply = answer(plane, Call("claim", {"supervisor": "box-1", "states": ["coding"]}))
    assert reply.ok, reply
    body: dict[str, Any] = reply.body
    return body


def test_answer_claims_and_reports_a_task(plane: ControlPlane) -> None:
    health = answer(plane, Call("health", {}))
    assert health.ok and health.body == plane.health()
    assert answer(plane, Call("claim", {"supervisor": "box-1", "states": ["coding"]})).body is None
    plane.start("T-1", TICKET)

    grant = TaskGrant.from_json(claim(plane))
    beat = answer(plane, Call("heartbeat", {"supervisor": "box-1", "tasks": [grant.task, 99]}))
    note = answer(plane, Call("note", {"task": grant.task, "text": "started", "attempt": 1}))
    memory = answer(
        plane,
        Call("set_memory", {"task": grant.task, "key": "session", "value": "s", "attempt": 1}),
    )
    outcome = Outcome(kind="event", event="stuck", attempt=1).to_json()
    report = answer(
        plane, Call("report", {"task": grant.task, "outcome": outcome, "supervisor": "box-1"})
    )

    assert beat.ok and beat.body == {"lost": [99]}
    assert note.ok and memory.ok and report.ok
    assert plane.get("T-1").status == "completed"


def test_the_same_request_gets_the_same_grant(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET)
    args = {"supervisor": "box-1", "states": ["coding"], "request": "r-1"}

    first = answer(plane, Call("claim", args))
    second = answer(plane, Call("claim", args))

    assert first.body == second.body


def test_answer_keeps_the_code_of_a_refusal(plane: ControlPlane) -> None:
    outcome = Outcome(kind="waiting").to_json()

    reply = answer(plane, Call("report", {"task": 99, "outcome": outcome, "supervisor": "b"}))

    assert (reply.ok, reply.code) == (False, "task_not_found")
    assert reply.message == "there is no task 99"


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (Call("claim", {"supervisor": "box-1", "states": "coding"}), "invalid_body"),
        (Call("claim", {"states": ["coding"]}), "invalid_argument"),
        (
            Call("claim", {"supervisor": "b", "states": [], "request": "no spaces"}),
            "invalid_argument",
        ),
        (Call("report", {"task": "one", "outcome": {"kind": "waiting"}}), "invalid_body"),
        (Call("report", {"task": 1, "outcome": {"kind": "exploded"}}), "invalid_body"),
        (Call("heartbeat", {"supervisor": "b", "tasks": [0]}), "invalid_body"),
        (Call("note", {"task": 1, "text": "", "attempt": 1}), "invalid_payload"),
        (Call("set_memory", {"task": 1, "key": "k", "attempt": 1}), "invalid_body"),
        (Call("note", {"task": 2**63, "text": "x"}), "task_not_found"),
    ],
)
def test_a_bad_argument_is_refused_with_its_code(
    plane: ControlPlane, call: Call, code: str
) -> None:
    reply = answer(plane, call)

    assert (reply.ok, reply.code) == (False, code), reply


def test_a_lost_hold_is_not_serving(plane: ControlPlane, tmp_path: Path) -> None:
    engine = create_engine(sqlite_url(tmp_path / "a.db"))
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE control_plane SET serving = 'elsewhere:1:x', since = :now"),
            {"now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")},
        )
    engine.dispose()
    with pytest.raises(Exception):  # noqa: B017 - the hold is gone; the clock says so
        plane.tick()

    reply = answer(plane, Call("health", {}))

    assert (reply.ok, reply.code) == (False, "not_serving")


def test_an_unexpected_error_is_one_sentence(
    plane: ControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(token: str) -> None:
        raise ZeroDivisionError("a bug")

    monkeypatch.setattr(plane, "heartbeat", broken)

    reply = answer(plane, Call("heartbeat", {"supervisor": "b", "tasks": [1]}))

    assert (reply.ok, reply.code, reply.message) == (
        False,
        "unexpected",
        "the call failed unexpectedly",
    )
