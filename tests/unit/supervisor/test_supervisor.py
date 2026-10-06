"""The supervisor's loop against a fake control plane: claim, run, send, report,
heartbeat, stop."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from stateous.model.context import Context
from stateous.model.contract import on_state
from stateous.model.wire import Outcome, Schedule
from stateous.supervisor.supervisor import outcome_of

from .fakes import (
    CODING,
    TRIAGE,
    FakeApi,
    Verdict,
    done,
    grant,
    make,
    nothing_runs,
    run_until,
    runs,
    scored,
    until,
)


def keep_memory(key: str, value: Any) -> None:
    return None


def keep_note(text: str) -> None:
    return None


def context(state: str = "triage") -> Context[Any]:
    return Context[Any](grant(1, state), save_memory=keep_memory, note=keep_note)


# ── what a routine returns ──────────────────────────────────────────────────


def test_an_allowed_event_is_reported_with_its_payload_and_the_context_writes() -> None:
    ctx = context()
    ctx["note"] = "looked"

    outcome = outcome_of(TRIAGE, scored(Verdict(score=3)), ctx)

    assert outcome == Outcome(
        kind="event", event="scored", payload={"score": 3}, context={"note": "looked"}
    )


def test_none_is_reported_as_waiting_with_the_timers() -> None:
    ctx = context("coding")
    ctx.schedule(done, after="10m")

    outcome = outcome_of(CODING, None, ctx)

    assert outcome == Outcome(
        kind="waiting", schedules=(Schedule(event="done", payload=None, after="10m"),)
    )


def test_an_event_the_state_may_not_emit_fails_the_task() -> None:
    outcome = outcome_of(TRIAGE, done(), context())

    assert outcome.kind == "failed"
    assert outcome.error == (
        "the routine returned 'done', which 'triage' may not emit (allowed: scored)"
    )


def test_a_value_that_is_not_an_event_or_none_fails_the_task() -> None:
    outcome = outcome_of(TRIAGE, "scored", context())

    assert outcome.kind == "failed"
    assert outcome.error == "the routine of 'triage' returned 'scored'; return an event or None"


def test_a_context_write_that_is_not_json_fails_the_task() -> None:
    @on_state(TRIAGE)
    async def triage(ctx: Context[Any]) -> scored:
        ctx["when"] = datetime(2026, 10, 6, tzinfo=UTC)
        return scored(Verdict(score=1))

    api = FakeApi(grant(1, "triage"))

    asyncio.run(run_until(make(api, triage), api.reported, 1))

    error = api.reports[1].error
    assert api.reports[1].kind == "failed"
    assert error is not None
    assert error.startswith("TypeError: ctx['when'] must be JSON: ")
    assert "datetime" in error


# ── running routines ────────────────────────────────────────────────────────


def test_a_def_routine_runs_in_its_own_thread_and_its_event_is_reported() -> None:
    threads: list[str] = []

    @on_state(TRIAGE)
    def triage(ctx: Context[Any]) -> scored:
        threads.append(threading.current_thread().name)
        ctx["note"] = "looked"
        return scored(Verdict(score=3))

    api = FakeApi(grant(1, "triage"))

    code = asyncio.run(run_until(make(api, triage), api.reported, 1))

    assert code == 0
    assert api.reports[1] == Outcome(
        kind="event", event="scored", payload={"score": 3}, context={"note": "looked"}
    )
    assert threads == ["stateous-routine-1"]
    assert api.called("claim", "box-1", ("triage",))


def test_an_async_routine_runs_on_the_loop() -> None:
    on_loop: list[bool] = []

    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        on_loop.append(asyncio.current_task() is not None)
        return done()

    api = FakeApi(grant(1, "coding"))

    asyncio.run(run_until(make(api, coding), api.reported, 1))

    assert on_loop == [True]
    assert api.reports[1] == Outcome(kind="event", event="done")


def test_a_routine_that_raises_fails_its_task_with_the_error() -> None:
    @on_state(TRIAGE)
    def triage(ctx: Context[Any]) -> scored:
        raise RuntimeError("the scanner is down")

    api = FakeApi(grant(1, "triage"))

    asyncio.run(run_until(make(api, triage), api.reported, 1))

    assert api.reports[1] == Outcome(kind="failed", error="RuntimeError: the scanner is down")


@pytest.mark.parametrize("is_async", [False, True])
def test_memory_writes_and_notes_are_sent_in_order_before_the_report(is_async: bool) -> None:
    def work(ctx: Context[Any]) -> done:
        assert ctx.memory["dev"] == "s-0"
        ctx.memory["dev"] = "s-1"
        ctx.note("one")
        ctx.note("two")
        del ctx.memory["dev"]
        return done()

    async def work_async(ctx: Context[Any]) -> done:
        return work(ctx)

    routine = on_state(CODING)(work_async if is_async else work)
    api = FakeApi(grant(1, "coding", memory={"dev": "s-0"}))
    api.note_delay = 0.02

    asyncio.run(run_until(make(api, routine), api.reported, 1))

    assert api.sends(1) == [
        ("memory", 1, "dev", "s-1"),
        ("note", 1, "one"),
        ("note", 1, "two"),
        ("memory", 1, "dev", None),
        ("report", 1, "event"),
    ]


def test_a_refused_note_is_logged_and_the_report_is_still_sent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        ctx.note("one")
        return done()

    api = FakeApi(grant(1, "coding"))
    api.refuse_notes = True

    asyncio.run(run_until(make(api, coding), api.reported, 1))

    assert "refused a note" in caplog.text


def test_no_more_than_tasks_routines_run_at_once() -> None:
    now: list[int] = []
    most = [0]

    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        now.append(ctx.task)
        most[0] = max(most[0], len(now))
        await asyncio.sleep(0.05)
        now.remove(ctx.task)
        return done()

    api = FakeApi(grant(1, "coding"), grant(2, "coding"), grant(3, "coding"))

    def all_reported() -> bool:
        return len(api.reports) == 3

    asyncio.run(run_until(make(api, coding, tasks=2), all_reported))

    assert most[0] == 2


def test_a_refused_report_is_logged_and_the_next_task_is_taken(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        return done()

    api = FakeApi(grant(1, "coding"), grant(2, "coding"))
    api.refuse_reports = True

    asyncio.run(run_until(make(api, coding), api.called, "report", 2, "event"))

    assert "task 1 of T-1: the control plane refused the report" in caplog.text


# ── heartbeats and lost tasks ───────────────────────────────────────────────


def test_the_heartbeat_names_the_running_tasks() -> None:
    gate = asyncio.Event()

    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        await gate.wait()
        return done()

    api = FakeApi(grant(1, "coding"))
    supervisor = make(api, coding)

    async def scenario() -> int:
        running = asyncio.create_task(supervisor.run())
        await until(api.called, "heartbeat", (1,))
        gate.set()
        await until(api.reported, 1)
        supervisor.on_signal()
        return await running

    assert asyncio.run(scenario()) == 0


def test_a_lost_task_cancels_its_async_routine_and_is_not_reported() -> None:
    cancelled: list[int] = []

    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(ctx.task)
            raise
        return done()

    api = FakeApi(grant(1, "coding"))
    api.lost = {1}
    supervisor = make(api, coding)

    async def scenario() -> int:
        running = asyncio.create_task(supervisor.run())
        await until(api.called, "heartbeat", (1,))
        await until(nothing_runs, supervisor)
        supervisor.on_signal()
        return await running

    assert asyncio.run(scenario()) == 0
    assert cancelled == [1]
    assert api.sends(1) == []


def test_a_lost_task_drops_its_thread_routines_result_and_later_sends() -> None:
    release = threading.Event()
    ended = threading.Event()

    @on_state(CODING)
    def coding(ctx: Context[Any]) -> done:
        release.wait(30)
        ctx.note("too late")
        ctx.memory["dev"] = "s-1"
        ended.set()
        return done()

    api = FakeApi(grant(1, "coding"))
    api.lost = {1}
    supervisor = make(api, coding)

    async def scenario() -> int:
        running = asyncio.create_task(supervisor.run())
        await until(api.called, "heartbeat", (1,))
        await until(nothing_runs, supervisor)
        release.set()
        await until(ended.is_set)
        for _ in range(5):  # let the loop run what the thread gave it
            await asyncio.sleep(0)
        supervisor.on_signal()
        return await running

    assert asyncio.run(scenario()) == 0
    assert api.sends(1) == []


# ── stopping ────────────────────────────────────────────────────────────────


def test_the_first_signal_stops_claims_and_waits_for_the_running_routine() -> None:
    gate = asyncio.Event()

    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        await gate.wait()
        return done()

    api = FakeApi(grant(1, "coding"), grant(2, "coding"))
    supervisor = make(api, coding, tasks=1)

    async def scenario() -> int:
        running = asyncio.create_task(supervisor.run())
        await until(runs, supervisor, 1)
        supervisor.on_signal()
        await asyncio.sleep(0.1)
        assert not running.done()
        claims = api.claims()
        gate.set()
        code = await running
        assert api.claims() == claims
        return code

    assert asyncio.run(scenario()) == 0
    assert api.reported(1)
    assert [g.task for g in api.grants] == [2]


def test_the_second_signal_cancels_the_routines_and_run_returns_1() -> None:
    cancelled: list[int] = []

    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(ctx.task)
            raise
        return done()

    api = FakeApi(grant(1, "coding"))
    supervisor = make(api, coding)

    code = asyncio.run(run_until(supervisor, runs, supervisor, 1, signals=2))

    assert code == 1
    assert cancelled == [1]
    assert api.reports == {}


def test_the_second_signal_does_not_wait_for_a_thread_routine() -> None:
    release = threading.Event()

    @on_state(CODING)
    def coding(ctx: Context[Any]) -> done:
        release.wait(30)
        return done()

    api = FakeApi(grant(1, "coding"))
    supervisor = make(api, coding)
    started = time.monotonic()

    code = asyncio.run(run_until(supervisor, runs, supervisor, 1, signals=2))

    release.set()
    assert code == 1
    assert time.monotonic() - started < 3
    assert api.reports == {}


def test_a_routine_slow_to_stop_is_left_after_cancel_wait(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(0.5)  # a slow clean-up; then it ends
            raise
        return done()

    api = FakeApi(grant(1, "coding"))
    supervisor = make(api, coding, cancel_wait=0.1)

    async def scenario() -> tuple[int, float]:
        running = asyncio.create_task(supervisor.run())
        await until(runs, supervisor, 1)
        supervisor.on_signal()
        supervisor.on_signal()
        started = time.monotonic()
        code = await running
        return code, time.monotonic() - started

    caplog.set_level(logging.WARNING, logger="stateous.supervisor")
    code, took = asyncio.run(scenario())

    assert code == 1
    assert took < 0.4
    assert "1 routine(s) did not stop" in caplog.text


def test_a_signal_before_run_stops_it_at_once_without_a_claim() -> None:
    api = FakeApi(grant(1, "coding"))

    @on_state(CODING)
    async def coding(ctx: Context[Any]) -> done:
        return done()

    supervisor = make(api, coding)
    supervisor.on_signal()

    assert asyncio.run(supervisor.run()) == 0
    assert api.claims() == 0
