"""The supervisor and its outbox: each report, note and memory write is written to
the outbox first, then sent. It leaves the outbox when the control plane answered
it for good. At start, the supervisor sends what is left, before it claims."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from stateous.model.context import Context
from stateous.model.contract import on_state
from stateous.model.wire import Outcome
from stateous.supervisor.outbox import Kind, Outbox
from stateous.supervisor.remote import Refused

from .fakes import CODING, FakeApi, done, grant, make, nothing_runs, run_until, until


@on_state(CODING)
async def coding(ctx: Context[Any]) -> done:
    ctx.memory["dev"] = "s-1"
    ctx.note("one")
    return done()


def kinds(outbox: Outbox) -> list[tuple[int, Kind]]:
    return [(message.task, message.kind) for message in outbox.pending()]


class WatchingApi(FakeApi):
    """Records what the outbox holds at each send."""

    def __init__(self, outbox: Outbox, *grants: Any) -> None:
        super().__init__(*grants)
        self.outbox = outbox
        self.seen: list[list[tuple[int, Kind]]] = []

    async def report(
        self, task: int, outcome: Outcome, *, supervisor: str, stop: asyncio.Event | None = None
    ) -> None:
        self.seen.append(kinds(self.outbox))
        await super().report(task, outcome, supervisor=supervisor, stop=stop)

    async def note(
        self, task: int, text: str, *, attempt: int, stop: asyncio.Event | None = None
    ) -> None:
        self.seen.append(kinds(self.outbox))
        await super().note(task, text, attempt=attempt, stop=stop)

    async def set_memory(
        self,
        task: int,
        key: str,
        value: Any,
        *,
        attempt: int,
        stop: asyncio.Event | None = None,
    ) -> None:
        self.seen.append(kinds(self.outbox))
        await super().set_memory(task, key, value, attempt=attempt, stop=stop)


def test_each_message_is_in_the_outbox_when_it_is_sent_and_leaves_after_it() -> None:
    with Outbox.in_memory() as outbox:
        api = WatchingApi(outbox, grant(1, "coding"))

        asyncio.run(run_until(make(api, coding, outbox=outbox), api.reported, 1))

        assert api.seen == [  # the routine wrote both before the first send
            [(1, "memory"), (1, "note")],
            [(1, "note")],
            [(1, "report")],
        ]
        assert outbox.pending() == []


def test_the_failure_report_after_a_refusal_goes_through_the_outbox_too() -> None:
    with Outbox.in_memory() as outbox:
        api = WatchingApi(outbox, grant(1, "coding"))
        api.refusals = [Refused(422, "the context changes break the schema")]

        asyncio.run(run_until(make(api, coding, outbox=outbox), api.reported, 1))

        assert api.seen[-2:] == [[(1, "report")], [(1, "report")]]
        assert api.reports[1].kind == "failed"
        assert outbox.pending() == []


def test_an_unreachable_control_plane_keeps_the_messages_and_the_stop_does_not_wait(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with Outbox.in_memory() as outbox:
        api = FakeApi(grant(1, "coding"))
        api.down = True
        supervisor = make(api, coding, outbox=outbox)

        def tried(times: int) -> bool:
            return api.tries >= times

        async def scenario() -> int:
            running = asyncio.create_task(supervisor.run())
            await until(tried, 3)
            supervisor.on_signal()
            return await asyncio.wait_for(running, 5.0)

        caplog.set_level(logging.WARNING, logger="stateous.supervisor")
        assert asyncio.run(scenario()) == 0
        assert kinds(outbox) == [(1, "memory"), (1, "note"), (1, "report")]
        assert [message.attempt for message in outbox.pending()] == [1, 1, 1]
        assert api.sends(1) == []
        assert "stays in the outbox" in caplog.text


def test_at_start_the_messages_left_are_sent_in_order_before_the_first_claim() -> None:
    with Outbox.in_memory() as outbox:
        outbox.append(1, 1, "T-1", "memory", {"key": "dev", "value": "s-1"})
        outbox.append(2, 1, "T-2", "note", {"text": "two"})
        outbox.append(1, 1, "T-1", "note", {"text": "one"})
        outbox.append(1, 1, "T-1", "report", Outcome(kind="event", event="done").to_json())
        api = FakeApi()

        def claimed() -> bool:
            return api.claims() >= 1

        asyncio.run(run_until(make(api, coding, outbox=outbox), claimed))

        assert api.calls[:5] == [
            ("memory", 1, "dev", "s-1"),
            ("note", 2, "two"),
            ("note", 1, "one"),
            ("report", 1, "event"),
            ("claim", "box-1", ("coding",)),
        ]
        assert api.attempts[:4] == [
            ("memory", 1, 1),
            ("note", 2, 1),
            ("note", 1, 1),
            ("report", 1, 1),
        ]
        assert outbox.pending() == []


def test_a_message_left_for_a_task_that_moved_on_is_dropped_with_one_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with Outbox.in_memory() as outbox:
        outbox.append(1, 1, "T-1", "report", Outcome(kind="event", event="done").to_json())
        api = FakeApi()
        api.refusals = [Refused(409, "task 1 is at attempt 2, not attempt 1")]

        def claimed() -> bool:
            return api.claims() >= 1

        asyncio.run(run_until(make(api, coding, outbox=outbox), claimed))

        assert api.sends(1) == [("report", 1, "event")]  # no failure report for an old attempt
        assert outbox.pending() == []
        refused = [r for r in caplog.records if "refused the report" in r.getMessage()]
        assert [r.getMessage() for r in refused] == [
            "task 1 of T-1: the control plane refused the report: "
            "task 1 is at attempt 2, not attempt 1"
        ]


def test_a_stop_while_the_left_messages_cannot_be_sent_keeps_them_and_claims_nothing() -> None:
    with Outbox.in_memory() as outbox:
        outbox.append(1, 1, "T-1", "note", {"text": "one"})
        outbox.append(1, 1, "T-1", "report", Outcome(kind="event", event="done").to_json())
        api = FakeApi(grant(2, "coding"))
        api.down = True
        supervisor = make(api, coding, outbox=outbox)

        def tried() -> bool:
            return api.tries >= 2

        assert asyncio.run(run_until(supervisor, tried)) == 0
        assert kinds(outbox) == [(1, "note"), (1, "report")]
        assert api.claims() == 0


class LosingApi(FakeApi):
    """Holds the first note until the task is lost at a heartbeat."""

    def __init__(self) -> None:
        super().__init__(grant(1, "coding"))
        self.lost = {1}
        self.release = asyncio.Event()

    async def note(
        self, task: int, text: str, *, attempt: int, stop: asyncio.Event | None = None
    ) -> None:
        await self.release.wait()
        await super().note(task, text, attempt=attempt, stop=stop)


def test_the_messages_of_a_task_the_control_plane_took_back_leave_the_outbox() -> None:
    with Outbox.in_memory() as outbox:
        api = LosingApi()
        supervisor = make(api, coding, outbox=outbox)

        async def scenario() -> int:
            running = asyncio.create_task(supervisor.run())
            await until(api.called, "heartbeat", (1,))
            await until(nothing_runs, supervisor)
            supervisor.on_signal()
            return await asyncio.wait_for(running, 5.0)

        assert asyncio.run(scenario()) == 0
        assert outbox.pending() == []
        assert api.sends(1) == [("memory", 1, "dev", "s-1")]
