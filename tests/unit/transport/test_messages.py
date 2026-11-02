"""The neutral messages of a transport: a call, and its reply."""

import subprocess
import sys
from typing import Any

import pytest

from stateous.model.wire import WireError
from stateous.transport import Call, Reply

ARGS: dict[str, dict[str, Any]] = {
    "health": {},
    "claim": {"supervisor": "box-1", "states": ["coding"], "request": "r1", "wait": 5},
    "report": {"task": 1, "outcome": {"kind": "waiting"}, "supervisor": "box-1"},
    "heartbeat": {"supervisor": "box-1", "tasks": [1, 2]},
    "note": {"task": 1, "text": "started", "attempt": 1},
    "set_memory": {"task": 1, "key": "session", "value": "s-1", "attempt": 1},
}


@pytest.mark.parametrize("name", sorted(ARGS))
def test_a_call_goes_through_json_unchanged(name: str) -> None:
    call = Call(name, ARGS[name])  # pyright: ignore[reportArgumentType]

    assert Call.from_json(call.to_json()) == call


def test_a_reply_goes_through_json_unchanged() -> None:
    good = Reply(ok=True, body={"lost": [2]})
    bad = Reply(ok=False, code="task_not_held", message="the task is not yours")

    assert Reply.from_json(good.to_json()) == good
    assert Reply.from_json(bad.to_json()) == bad
    assert bad.to_json() == {
        "ok": False,
        "code": "task_not_held",
        "message": "the task is not yours",
        "body": None,
    }


@pytest.mark.parametrize(
    "data",
    [
        {"name": "explode", "args": {}},
        {"name": "claim", "args": ["box-1"]},
        {"args": {}},
        "claim",
    ],
)
def test_a_bad_call_is_a_wire_error(data: Any) -> None:
    with pytest.raises(WireError):
        Call.from_json(data)


@pytest.mark.parametrize(
    "data",
    [
        {"ok": "yes"},
        {"ok": False, "message": "no code"},
        {"ok": False, "code": "task_not_held"},
        [],
    ],
)
def test_a_bad_reply_is_a_wire_error(data: Any) -> None:
    with pytest.raises(WireError):
        Reply.from_json(data)


def test_transport_imports_nothing_of_either_side() -> None:
    probe = (
        "import sys, stateous.transport; "
        "print(sorted(m for m in sys.modules "
        "if m.startswith(('stateous.control', 'stateous.supervisor'))))"
    )
    found = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, encoding="utf-8", check=True
    )
    assert found.stdout.strip() == "[]"
