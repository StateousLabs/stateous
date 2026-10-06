"""The supervisor's loop at its edges: a task granted again while its old routine
cleans up, a refused report, a claim in flight at a stop signal, a routine that
raises a BaseException, and `stop_now`."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from typing import Any

import pytest

from stateous.model.context import Context
from stateous.model.contract import on_state
from stateous.model.wire import Outcome
from stateous.supervisor import supervisor as supervisor_module
from stateous.supervisor.remote import Refused

from .fakes import CODING, FakeApi, done, grant, make, nothing_runs, run_until, runs, until

# ── a task granted again ────────────────────────────────────────────────────


class RegrantingApi(FakeApi):
    """Takes task 1 back at the first heartbeat, and grants it again (attempt 2) at
    once, as the engine does when a lease ends: the same task id."""

    def __init__(self) -> None:
        super().__init__(grant(1, "coding", attempt=1))
        self.lost = {1}

    async def heartbeat(self, supervisor: str, tasks: Collection[int]) -> list[int]:
        lost = await super().heartbeat(supervisor, tasks)
        if lost:
            self.lost = set()
            self.grants.append(grant(1, "coding", attempt=2))
        return lost

    def heartbeats(self) -> int:
        return sum(1 for call in self.calls if call[0] == "heartbeat")


def test_a_task_granted_again_while_its_old_routine_cleans_up_runs_and_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cleaned = asyncio.Event()
    finish = asyncio.Event()
    attempts: list[int] = []

    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        attempts.append(ctx.attempt)
        if ctx.attempt == 2:
            await finish.wait()
            return done()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await cleaned.wait()  # a slow clean-up, e.g. it stops an agent process
            raise
        return done()

    api = RegrantingApi()
    supervisor = make(api, coding, tasks=2)

    def second_attempt_runs() -> bool:
        return attempts == [1, 2]

    def beat_again(after: int) -> bool:
        return api.heartbeats() > after

    async def scenario() -> int:
        running = asyncio.create_task(supervisor.run())
        await until(second_attempt_runs)
        cleaned.set()  # the old routine ends while the new one runs
        for _ in range(5):
            await asyncio.sleep(0)
        assert supervisor.running == [1]
        await until(beat_again, api.heartbeats())
        beats = [call for call in api.calls if call[0] == "heartbeat"]
        assert beats[-1] == ("heartbeat", (1,))  # attempt 2 is still held
        finish.set()
        await until(api.reported, 1)
        supervisor.on_signal()
        return await asyncio.wait_for(running, 5.0)

    caplog.set_level(logging.WARNING)
    assert asyncio.run(scenario()) == 0
    assert api.reports[1] == Outcome(kind="event", event="done")
    assert api.sends(1) == [("report", 1, "event")]
    assert "Traceback" not in caplog.text
    assert "KeyError" not in caplog.text


# ── refused reports ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "refusal",
    [
        Refused(422, "the context changes break the schema: count: not an integer"),
        Refused(409, "'coding' may not emit 'done'; it may emit: stuck"),
    ],
)
def test_a_refused_report_fails_the_task_at_once_with_the_refusal(refusal: Refused) -> None:
    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        return done()

    api = FakeApi(grant(1, "coding"))
    api.refusals = [refusal]

    asyncio.run(run_until(make(api, coding), api.reported, 1))

    assert api.sends(1) == [("report", 1, "event"), ("report", 1, "failed")]
    assert api.reports[1] == Outcome(
        kind="failed", error=f"the control plane refused the report: {refusal.message}"
    )


def test_a_report_refused_because_the_task_is_gone_is_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        return done()

    api = FakeApi(grant(1, "coding"))
    api.refusals = [Refused(404, "there is no task 1")]
    supervisor = make(api, coding)

    async def scenario() -> int:
        running = asyncio.create_task(supervisor.run())
        await until(api.called, "report", 1, "event")
        await until(nothing_runs, supervisor)
        supervisor.on_signal()
        return await asyncio.wait_for(running, 5.0)

    assert asyncio.run(scenario()) == 0
    assert api.sends(1) == [("report", 1, "event")]
    assert "refused the report: there is no task 1" in caplog.text


class NotHeldApi(FakeApi):
    """Refuses the first report with 409: the task is no longer this supervisor's."""

    async def report(self, task: int, outcome: Outcome, *, supervisor: str) -> None:
        if not self.lost:
            self.lost = {task}
            self.calls.append(("report", task, outcome.kind))
            raise Refused(409, f"task {task} is not held by 'box-1'")
        await super().report(task, outcome, supervisor=supervisor)


def test_a_report_refused_because_the_task_is_not_held_is_dropped() -> None:
    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        return done()

    api = NotHeldApi(grant(1, "coding"))
    supervisor = make(api, coding)

    async def scenario() -> int:
        running = asyncio.create_task(supervisor.run())
        await until(api.called, "report", 1, "event")
        await until(nothing_runs, supervisor)
        supervisor.on_signal()
        return await asyncio.wait_for(running, 5.0)

    assert asyncio.run(scenario()) == 0
    assert api.sends(1) == [("report", 1, "event")]


def test_a_failure_report_that_is_refused_too_is_logged_and_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        return done()

    api = FakeApi(grant(1, "coding"))
    api.refusals = [Refused(422, "first"), Refused(422, "second")]
    supervisor = make(api, coding)

    async def scenario() -> int:
        running = asyncio.create_task(supervisor.run())
        await until(api.called, "report", 1, "failed")
        await until(nothing_runs, supervisor)
        supervisor.on_signal()
        return await asyncio.wait_for(running, 5.0)

    assert asyncio.run(scenario()) == 0
    assert api.sends(1) == [("report", 1, "event"), ("report", 1, "failed")]
    assert "refused the report: first" in caplog.text
    assert "refused the failure report too: second" in caplog.text


# ── a claim in flight at a stop signal ──────────────────────────────────────


def claim_waits(api: FakeApi) -> bool:
    return api.claim_waits == 1


def test_a_claim_in_flight_at_the_first_signal_finishes_and_its_task_runs() -> None:
    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        return done()

    api = FakeApi(grant(1, "coding"))
    gate = api.claim_gate = asyncio.Event()
    supervisor = make(api, coding)

    async def scenario() -> int:
        running = asyncio.create_task(supervisor.run())
        await until(claim_waits, api)
        supervisor.on_signal()
        for _ in range(5):
            await asyncio.sleep(0)
        assert api.claim_waits == 1  # the claim was not cancelled
        gate.set()
        return await asyncio.wait_for(running, 5.0)

    assert asyncio.run(scenario()) == 0
    assert api.reports[1] == Outcome(kind="event", event="done")
    assert api.claims() == 1


def test_the_second_signal_cancels_a_claim_in_flight() -> None:
    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        return done()

    api = FakeApi(grant(1, "coding"))
    api.claim_gate = asyncio.Event()
    supervisor = make(api, coding)

    async def scenario() -> int:
        running = asyncio.create_task(supervisor.run())
        await until(claim_waits, api)
        supervisor.on_signal()
        supervisor.on_signal()
        return await asyncio.wait_for(running, 5.0)

    assert asyncio.run(scenario()) == 1
    assert api.claim_waits == 0
    assert api.reports == {}


# ── errors that are not plain exceptions ────────────────────────────────────


@pytest.mark.parametrize("error", [SystemExit(3), KeyboardInterrupt()])
def test_an_async_routine_that_raises_a_base_exception_fails_its_task(
    error: BaseException,
) -> None:
    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        raise error

    api = FakeApi(grant(1, "coding"), grant(2, "coding"))

    code = asyncio.run(run_until(make(api, coding), api.reported, 2))

    assert code == 0
    assert api.reports[1] == Outcome(kind="failed", error=f"{type(error).__name__}: {error}")


def test_an_error_after_the_routine_still_fails_the_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(state: Any, value: object, ctx: Context[Any]) -> Outcome:
        raise ValueError("cannot read the payload")

    monkeypatch.setattr(supervisor_module, "outcome_of", broken)

    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        return done()

    api = FakeApi(grant(1, "coding"))

    asyncio.run(run_until(make(api, coding), api.reported, 1))

    assert api.reports[1] == Outcome(kind="failed", error="ValueError: cannot read the payload")


# ── stop_now ────────────────────────────────────────────────────────────────


def test_stop_now_cancels_the_routines_without_a_signal_again_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        await asyncio.sleep(3600)
        return done()

    api = FakeApi(grant(1, "coding"))
    supervisor = make(api, coding)

    async def scenario() -> int:
        running = asyncio.create_task(supervisor.run())
        await until(runs, supervisor, 1)
        supervisor.stop_now()
        return await asyncio.wait_for(running, 5.0)

    caplog.set_level(logging.WARNING, logger="stateous.supervisor")
    assert asyncio.run(scenario()) == 1
    assert "stopping now: cancelling 1 routine(s)" in caplog.text
    assert "signal again" not in caplog.text
    assert api.reports == {}


def test_stop_now_before_run_stops_it_at_once() -> None:
    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        return done()

    api = FakeApi(grant(1, "coding"))
    supervisor = make(api, coding)
    supervisor.stop_now()

    assert asyncio.run(supervisor.run()) == 1
    assert api.claims() == 0
