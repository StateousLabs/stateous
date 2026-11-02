"""`CallingTransport`: a transport made of one function that sends a call."""

import asyncio
from typing import Any

import pytest

from stateous.model.wire import Outcome, WireError
from stateous.transport import (
    CONFLICT_CODES,
    RETRY_CODES,
    Call,
    CallingTransport,
    NotReached,
    Refused,
    Reply,
)

GRANT: dict[str, Any] = {
    "task": 7,
    "token": "T-1",
    "state": "coding",
    "visit": 1,
    "attempt": 1,
    "context": {"repo": "acme/api"},
    "memory": {},
}


class Recorder:
    """A `send` that records each call and answers with the next reply."""

    def __init__(self, *replies: Reply) -> None:
        self.calls: list[Call] = []
        self.replies = list(replies)

    async def __call__(self, call: Call) -> Reply:
        self.calls.append(call)
        return self.replies.pop(0)


def test_each_call_is_encoded_as_the_kit_expects() -> None:
    send = Recorder(
        Reply(ok=True, body=GRANT),
        Reply(ok=True),
        Reply(ok=True, body={"lost": [3]}),
        Reply(ok=True),
        Reply(ok=True),
        Reply(ok=True, body={"workflow": "sdlc", "checksum": "c1"}),
    )
    transport = CallingTransport(send, where="a queue")

    async def run() -> None:
        grant = await transport.claim("box-1", ["coding"], request="r1", wait=5)
        assert grant is not None and grant.task == 7
        await transport.report(7, Outcome(kind="waiting"), supervisor="box-1")
        assert await transport.heartbeat("box-1", [3, 7]) == [3]
        await transport.note(7, "started", attempt=1)
        await transport.set_memory(7, "session", "s-1", attempt=1)
        assert await transport.health() == {"workflow": "sdlc", "checksum": "c1"}

    asyncio.run(run())

    assert send.calls[0] == Call(
        "claim", {"supervisor": "box-1", "states": ["coding"], "request": "r1", "wait": 5}
    )
    assert send.calls[1] == Call(
        "report",
        {"task": 7, "outcome": Outcome(kind="waiting").to_json(), "supervisor": "box-1"},
    )
    assert send.calls[2] == Call("heartbeat", {"supervisor": "box-1", "tasks": [3, 7]})
    assert send.calls[3] == Call("note", {"task": 7, "text": "started", "attempt": 1})
    assert send.calls[4] == Call(
        "set_memory", {"task": 7, "key": "session", "value": "s-1", "attempt": 1}
    )
    assert send.calls[5] == Call("health", {})
    assert transport.where == "a queue"


def test_an_empty_claim_is_none() -> None:
    transport = CallingTransport(Recorder(Reply(ok=True, body=None)), where="q")

    assert asyncio.run(transport.claim("box-1", ["coding"])) is None


def test_a_refusal_keeps_its_code() -> None:
    reply = Reply(ok=False, code="task_not_held", message="the task is not yours")
    transport = CallingTransport(Recorder(reply), where="q")

    with pytest.raises(Refused) as refused:
        asyncio.run(transport.note(1, "x", attempt=1))

    assert (refused.value.code, refused.value.message) == ("task_not_held", "the task is not yours")


@pytest.mark.parametrize("code", sorted(RETRY_CODES))
def test_a_retry_code_is_not_reached(code: str) -> None:
    transport = CallingTransport(Recorder(Reply(ok=False, code=code, message="later")), where="q")

    with pytest.raises(NotReached, match=f"{code}: later"):
        asyncio.run(transport.note(1, "x", attempt=1))


def test_a_send_that_does_not_reach_passes_it_on() -> None:
    async def lost(call: Call) -> Reply:
        raise NotReached("the broker is down")

    transport = CallingTransport(lost, where="q")

    with pytest.raises(NotReached, match="the broker is down"):
        asyncio.run(transport.health())


def test_the_codes_of_conflict() -> None:
    assert {
        "event_not_allowed",
        "token_not_active",
        "child_token",
        "task_not_held",
        "attempt_over",
    } == CONFLICT_CODES
    assert {"database_busy", "not_serving"} == RETRY_CODES


@pytest.mark.parametrize(
    ("reply", "call"),
    [
        (Reply(ok=True, body={"task": "seven"}), "claim"),
        (Reply(ok=True, body={"gone": []}), "heartbeat"),
        (Reply(ok=True, body=["not", "an", "object"]), "health"),
        (Reply(ok=False), "health"),
    ],
)
def test_a_reply_of_the_wrong_shape_is_refused_as_unexpected(reply: Reply, call: str) -> None:
    transport = CallingTransport(Recorder(reply), where="q")
    calls = {
        "claim": lambda: transport.claim("box-1", ["coding"]),
        "heartbeat": lambda: transport.heartbeat("box-1", [1]),
        "health": transport.health,
    }

    with pytest.raises(Refused) as refused:
        asyncio.run(calls[call]())

    assert refused.value.code == "unexpected"


def test_aclose_does_nothing() -> None:
    asyncio.run(CallingTransport(Recorder(), where="q").aclose())


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("no reply in 30 s"), OSError("the broker closed"), WireError("bad reply")],
)
def test_a_send_that_fails_on_the_way_is_not_reached(failure: Exception) -> None:
    async def failing(call: Call) -> Reply:
        raise failure

    with pytest.raises(NotReached):
        asyncio.run(CallingTransport(failing, where="q").note(1, "x", attempt=1))
