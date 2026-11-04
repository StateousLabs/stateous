"""`Watching`: a transport that passes each call on, and records the supervisor's tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping
from typing import Any

import pytest

from stateous.model.wire import Outcome, TaskGrant
from stateous.transport import Refused
from stateous.tryout.watch import Watching


class Fake:
    """A transport that grants one task, and refuses a report when told to."""

    where = "fake"

    def __init__(self) -> None:
        self.refuse = False
        self.calls: list[str] = []

    async def health(self) -> dict[str, Any]:
        return {"workflow": "job", "checksum": "x"}

    async def claim(
        self,
        supervisor: str,
        states: Collection[str],
        *,
        request: str | None = None,
        wait: float = 0,
        labels: Mapping[str, str] | None = None,
    ) -> TaskGrant | None:
        self.calls.append("claim")
        return TaskGrant(7, "T-1", "work", 1, 2, {}, {}, None)

    async def report(self, task: int, outcome: Outcome, *, supervisor: str) -> None:
        self.calls.append("report")
        if self.refuse:
            raise Refused("attempt_over", "the attempt is over")

    async def heartbeat(self, supervisor: str, tasks: Collection[int]) -> list[int]:
        self.calls.append("heartbeat")
        return []

    async def note(self, task: int, text: str, *, attempt: int) -> None:
        self.calls.append("note")

    async def set_memory(self, task: int, key: str, value: Any, *, attempt: int) -> None:
        self.calls.append("set_memory")

    async def aclose(self) -> None:
        self.calls.append("aclose")


def test_a_grant_opens_a_step_and_the_states_are_served() -> None:
    watching = Watching(Fake())
    asyncio.run(watching.claim("s", ["work", "review"]))
    (step,) = watching.steps
    assert (step.task, step.token, step.state, step.attempt) == (7, "T-1", "work", 2)
    assert step.ended is None and step.outcome is None
    assert watching.served == {"work", "review"}
    assert watching.running() == [step]


def test_a_note_is_kept_and_counts_as_a_change() -> None:
    watching = Watching(Fake())
    asyncio.run(watching.claim("s", ["work"]))
    before = watching.last_change
    asyncio.run(watching.note(7, "half done", attempt=2))
    assert watching.steps[0].notes == ["half done"]
    assert watching.last_change >= before


def test_a_heartbeat_counts_as_a_change() -> None:
    watching = Watching(Fake())
    watching.last_change = 0.0
    asyncio.run(watching.heartbeat("s", [7]))
    assert watching.last_change > 0.0


@pytest.mark.parametrize(
    ("outcome", "text"),
    [
        (Outcome("event", event="done", attempt=2), "event done"),
        (Outcome("waiting", attempt=2), "waiting"),
        (Outcome("failed", error="KeyError: 'x'", attempt=2), "failed: KeyError: 'x'"),
    ],
)
def test_a_report_closes_the_step(outcome: Outcome, text: str) -> None:
    watching = Watching(Fake())
    asyncio.run(watching.claim("s", ["work"]))
    asyncio.run(watching.report(7, outcome, supervisor="s"))
    step = watching.steps[0]
    assert step.outcome == text
    assert step.ended is not None
    assert watching.running() == []


def test_a_refused_report_closes_the_step_and_is_raised_again() -> None:
    fake = Fake()
    fake.refuse = True
    watching = Watching(fake)
    asyncio.run(watching.claim("s", ["work"]))
    with pytest.raises(Refused):
        asyncio.run(watching.report(7, Outcome("event", event="done", attempt=2), supervisor="s"))
    assert watching.steps[0].outcome == "refused: the attempt is over"


def test_every_call_goes_to_the_inner_transport() -> None:
    fake = Fake()
    watching = Watching(fake)

    async def all_calls() -> None:
        await watching.health()
        await watching.claim("s", ["work"])
        await watching.heartbeat("s", [7])
        await watching.note(7, "n", attempt=2)
        await watching.set_memory(7, "k", 1, attempt=2)
        await watching.report(7, Outcome("waiting", attempt=2), supervisor="s")
        await watching.aclose()

    asyncio.run(all_calls())
    assert fake.calls == ["claim", "heartbeat", "note", "set_memory", "report", "aclose"]
    assert watching.where == "fake"


def test_calls_for_an_unknown_task_pass_on() -> None:
    fake = Fake()
    watching = Watching(fake)

    async def calls() -> None:
        await watching.claim("s", ["work"])
        await watching.note(99, "n", attempt=1)
        await watching.report(99, Outcome("waiting"), supervisor="s")

    asyncio.run(calls())
    assert watching.steps[0].notes == []
    assert watching.steps[0].ended is None
    assert fake.calls == ["claim", "note", "report"]


def test_a_report_without_an_attempt_closes_the_open_step() -> None:
    watching = Watching(Fake())

    async def calls() -> None:
        await watching.claim("s", ["work"])
        await watching.claim("s", ["work"])
        await watching.report(7, Outcome("waiting", attempt=2), supervisor="s")
        await watching.report(7, Outcome("waiting"), supervisor="s")

    asyncio.run(calls())
    assert [step.outcome for step in watching.steps] == ["waiting", "waiting"]
