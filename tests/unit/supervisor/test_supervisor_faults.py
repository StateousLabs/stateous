"""The supervisor when something goes wrong: a heartbeat that fails, a grant that comes
too late or has no routine, a send that fails, an outbox that fails, and a start that
is refused."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import sqlite3
import threading
from collections.abc import Collection
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from stateous.model.context import Context
from stateous.model.contract import on_state
from stateous.model.wire import Outcome
from stateous.supervisor import supervisor as supervisor_module
from stateous.supervisor.folder import Folder, FolderConfig
from stateous.supervisor.outbox import Kind, Message, Outbox
from stateous.supervisor.remote import GaveUp, Refused
from stateous.supervisor.supervisor import (
    Supervisor,
    SupervisorError,
    _Sends,  # pyright: ignore[reportPrivateUsage]
    open_supervisor,
)

from .fakes import CODING, FakeApi, done, grant, make, run_until, until

LOGGER = "stateous.supervisor"


@on_state(CODING)
async def coding(ctx: Context[Any]) -> done:
    return done()


def test_a_supervisor_needs_at_least_one_task() -> None:
    with pytest.raises(ValueError, match="tasks must be 1 or more"):
        make(FakeApi(), coding, tasks=0)


# ── heartbeats ──────────────────────────────────────────────────────────────


class BrokenHeartbeatApi(FakeApi):
    """The first heartbeat fails with `error`. The later ones work."""

    def __init__(self, error: Exception, *grants: Any) -> None:
        super().__init__(*grants)
        self.error = error
        self.failed = False

    async def heartbeat(
        self, supervisor: str, tasks: Collection[int], *, stop: asyncio.Event | None = None
    ) -> list[int]:
        if not self.failed:
            self.failed = True
            self.calls.append(("heartbeat", tuple(sorted(tasks))))
            raise self.error
        return await super().heartbeat(supervisor, tasks, stop=stop)

    def beats(self) -> int:
        return sum(1 for call in self.calls if call[0] == "heartbeat")


@pytest.mark.parametrize(
    ("error", "line"),
    [
        (Refused(403, "the token is not valid"), "the control plane refused a heartbeat: "),
        (RuntimeError("boom"), "a heartbeat failed"),
    ],
    ids=["refused", "failed"],
)
def test_a_heartbeat_that_fails_is_logged_and_the_next_heartbeat_is_sent(
    error: Exception, line: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> int:
        release = asyncio.Event()

        @on_state(CODING)
        async def waiting(ctx: Context[Any]) -> done:
            await release.wait()
            return done()

        api = BrokenHeartbeatApi(error, grant(1, "coding"))
        supervisor = make(api, waiting)
        running = asyncio.create_task(supervisor.run())
        await until(lambda: api.beats() >= 2)
        release.set()
        await until(api.reported, 1)
        supervisor.on_signal()
        code = await asyncio.wait_for(running, 5.0)
        assert api.reports[1] == Outcome(kind="event", event="done")
        return code

    caplog.set_level(logging.WARNING, logger=LOGGER)

    assert asyncio.run(scenario()) == 0
    assert line in caplog.text


# ── grants ──────────────────────────────────────────────────────────────────


def test_a_grant_that_comes_after_the_forced_stop_does_not_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = FakeApi(grant(1, "coding"))
    gate = api.claim_gate = asyncio.Event()
    supervisor = make(api, coding)

    def claim_waits() -> bool:
        return api.claim_waits == 1

    async def scenario() -> int:
        running = asyncio.create_task(supervisor.run())
        await until(claim_waits)
        supervisor.stop_now()
        gate.set()  # the control plane grants the task when the stop is forced already
        return await asyncio.wait_for(running, 5.0)

    caplog.set_level(logging.WARNING, logger=LOGGER)

    assert asyncio.run(scenario()) == 1
    assert api.reports == {}
    assert "task 1 of T-1: stopping now, so its routine does not run" in caplog.text


def test_a_claim_with_no_task_at_a_stop_ends_the_run_at_once() -> None:
    api = FakeApi()
    gate = api.claim_gate = asyncio.Event()
    supervisor = make(api, coding)

    def claim_waits() -> bool:
        return api.claim_waits == 1

    async def scenario() -> int:
        running = asyncio.create_task(supervisor.run())
        await until(claim_waits)
        supervisor.on_signal()
        gate.set()
        return await asyncio.wait_for(running, 5.0)

    assert asyncio.run(scenario()) == 0
    assert api.claims() == 1


def test_a_third_signal_does_not_force_the_stop_again(caplog: pytest.LogCaptureFixture) -> None:
    api = FakeApi()
    caplog.set_level(logging.WARNING, logger=LOGGER)

    def claimed() -> bool:
        return api.claims() >= 1

    assert asyncio.run(run_until(make(api, coding), claimed, signals=3)) == 1
    assert caplog.text.count("stopping now: cancelling") == 1


def test_a_grant_for_a_state_with_no_routine_fails_the_task() -> None:
    api = FakeApi(grant(1, "triage"))

    asyncio.run(run_until(make(api, coding), api.reported, 1))

    assert api.reports[1] == Outcome(
        kind="failed", error="this supervisor has no routine for 'triage'"
    )


def test_a_routine_that_ends_after_the_task_was_taken_back_is_not_reported() -> None:
    cancelled = asyncio.Event()

    @on_state(CODING)
    async def stubborn(ctx: Context[Any]) -> done:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()  # the routine ignores the cancel and returns
        return done()

    api = FakeApi(grant(1, "coding"))
    api.lost = {1}
    supervisor = make(api, stubborn)

    def nothing_runs() -> bool:
        return supervisor.running == []

    async def scenario() -> int:
        running = asyncio.create_task(supervisor.run())
        await until(cancelled.is_set)
        await until(nothing_runs)
        supervisor.on_signal()
        return await asyncio.wait_for(running, 5.0)

    assert asyncio.run(scenario()) == 0
    assert api.reports == {}


# ── what a routine returns or raises ────────────────────────────────────────


def test_a_long_value_in_the_failure_text_is_cut_to_100_characters() -> None:
    @on_state(CODING)
    async def wrong(ctx: Context[Any]) -> Any:
        return "x" * 200

    api = FakeApi(grant(1, "coding"))

    asyncio.run(run_until(make(api, wrong), api.reported, 1))

    assert api.reports[1] == Outcome(
        kind="failed",
        error="the routine of 'coding' returned "
        + repr("x" * 200)[:97]
        + "...; return an event or None",
    )


def test_a_def_routine_that_raises_a_base_exception_fails_its_task() -> None:
    @on_state(CODING)
    def leaving(ctx: Context[Any]) -> done:
        raise SystemExit(3)

    api = FakeApi(grant(1, "coding"))

    asyncio.run(run_until(make(api, leaving), api.reported, 1))

    assert api.reports[1] == Outcome(kind="failed", error="RuntimeError: SystemExit: 3")


# ── sends that fail ─────────────────────────────────────────────────────────


class FailingSendApi(FakeApi):
    """Notes and reports fail with `error`."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def note(
        self, task: int, text: str, *, attempt: int, stop: asyncio.Event | None = None
    ) -> None:
        raise self.error

    async def report(
        self, task: int, outcome: Outcome, *, supervisor: str, stop: asyncio.Event | None = None
    ) -> None:
        raise self.error


def test_a_note_that_fails_in_an_unknown_way_is_logged_and_leaves_the_outbox(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with Outbox.in_memory() as outbox:
        message = outbox.append(1, 1, "T-1", "note", {"text": "one"})
        supervisor = make(FailingSendApi(RuntimeError("boom")), coding, outbox=outbox)
        caplog.set_level(logging.WARNING, logger=LOGGER)

        assert asyncio.run(supervisor.send(message)) is True

        assert outbox.pending() == []
    assert "task 1: could not send a note" in caplog.text


def test_a_report_that_fails_in_an_unknown_way_is_logged_and_leaves_the_outbox(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with Outbox.in_memory() as outbox:
        message = outbox.append(1, 1, "T-1", "report", Outcome(kind="waiting").to_json())
        supervisor = make(FailingSendApi(RuntimeError("boom")), coding, outbox=outbox)
        caplog.set_level(logging.WARNING, logger=LOGGER)

        assert asyncio.run(supervisor.send(message)) is True

        assert outbox.pending() == []
    assert "task 1 of T-1: could not report" in caplog.text


def test_a_refused_report_whose_check_fails_in_an_unknown_way_is_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenCheckApi(FakeApi):
        async def heartbeat(
            self, supervisor: str, tasks: Collection[int], *, stop: asyncio.Event | None = None
        ) -> list[int]:
            raise RuntimeError("boom")

    with Outbox.in_memory() as outbox:
        message = outbox.append(1, 1, "T-1", "report", {"kind": "event", "event": "done"})
        api = BrokenCheckApi()
        api.refusals = [Refused(409, "task 1 is not this supervisor's", code="task_not_held")]
        supervisor = make(api, coding, outbox=outbox)
        caplog.set_level(logging.WARNING, logger=LOGGER)

        assert asyncio.run(supervisor.send(message)) is True

        assert outbox.pending() == []
    assert "task 1 of T-1: could not check the task" in caplog.text


def test_a_report_that_gives_up_at_the_stop_stays_in_the_outbox() -> None:
    class GivingUpReportApi(FakeApi):
        async def report(
            self,
            task: int,
            outcome: Outcome,
            *,
            supervisor: str,
            stop: asyncio.Event | None = None,
        ) -> None:
            raise GaveUp

    with Outbox.in_memory() as outbox:
        message = outbox.append(1, 1, "T-1", "report", Outcome(kind="waiting").to_json())
        supervisor = make(GivingUpReportApi(), coding, outbox=outbox)

        assert asyncio.run(supervisor.send(message)) is False

        assert outbox.pending() == [message]


class GivingUpApi(FakeApi):
    """The first note gives up, as a note does when the stop is set."""

    def __init__(self) -> None:
        super().__init__()
        self.given_up = False

    async def note(
        self, task: int, text: str, *, attempt: int, stop: asyncio.Event | None = None
    ) -> None:
        if not self.given_up:
            self.given_up = True
            raise GaveUp
        await super().note(task, text, attempt=attempt, stop=stop)


def test_after_a_message_that_stays_the_later_messages_of_its_task_stay_too() -> None:
    with Outbox.in_memory() as outbox:
        first = outbox.append(1, 1, "T-1", "note", {"text": "one"})
        second = outbox.append(1, 1, "T-1", "note", {"text": "two"})
        outbox.append(2, 1, "T-2", "note", {"text": "other"})
        api = GivingUpApi()

        def sent() -> bool:
            return api.called("note", 2, "other") and api.claims() >= 1

        asyncio.run(run_until(make(api, coding, outbox=outbox), sent))

        assert outbox.pending() == [first, second]
        assert not api.called("note", 1, "two")


# ── an outbox that fails on a report ────────────────────────────────────────


class BrokenReportOutbox(Outbox):
    """The outbox cannot take a report: it raises `error`."""

    error: Exception = sqlite3.OperationalError("database or disk is full")

    def append(self, task: int, attempt: int, token: str, kind: Kind, body: Any) -> Message:
        if kind == "report":
            raise self.error
        return super().append(task, attempt, token, kind, body)


class OutboxWithUnknownFault(BrokenReportOutbox):
    error = RuntimeError("boom")


def test_a_report_that_the_outbox_cannot_write_is_logged_and_not_sent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)

    def logged() -> bool:
        return "cannot write the report to the outbox" in caplog.text

    with BrokenReportOutbox.in_memory() as outbox:
        api = FakeApi(grant(1, "coding"))

        asyncio.run(run_until(make(api, coding, outbox=outbox), logged))

        assert api.reports == {}
    assert "task 1 of T-1: cannot write the report to the outbox: database or disk is full" in (
        caplog.text
    )


def test_a_report_that_fails_in_an_unknown_way_in_the_outbox_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)

    def logged() -> bool:
        return "could not finish" in caplog.text

    with OutboxWithUnknownFault.in_memory() as outbox:
        api = FakeApi(grant(1, "coding"))

        asyncio.run(run_until(make(api, coding, outbox=outbox), logged))

        assert api.reports == {}
    assert "task 1 of T-1: could not finish" in caplog.text


# ── a write from a thread, with a loop that is busy or closed ───────────────


def sends_of(supervisor: Supervisor) -> _Sends:
    return _Sends(supervisor, grant(1, "coding"))


def test_a_note_from_a_thread_after_the_loop_closed_is_dropped() -> None:
    supervisor = make(FakeApi(), coding)

    async def open_sends() -> _Sends:
        return sends_of(supervisor)

    sends = asyncio.run(open_sends())  # the loop is closed now
    worker = threading.Thread(target=sends.note, args=("late",))
    worker.start()
    worker.join(5.0)

    assert not worker.is_alive()
    assert supervisor.outbox.pending() == []


class HeldLoop:
    """A loop that keeps the calls from other threads until `release`."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.held: list[Any] = []

    def call_soon_threadsafe(self, call: Any) -> None:
        self.held.append(call)

    def release(self) -> None:
        for call in self.held:
            self.loop.call_soon_threadsafe(call)


class WatchedFuture(concurrent.futures.Future[None]):
    """A future that tells when a wait for its result ran out of time."""

    timed_out = threading.Event()

    def result(self, timeout: float | None = None) -> None:
        try:
            return super().result(timeout)
        except TimeoutError:
            self.timed_out.set()
            raise


@pytest.mark.parametrize("closes", [False, True], ids=["written later", "closed meanwhile"])
def test_a_note_from_a_thread_waits_for_a_busy_loop_and_stops_waiting_at_the_close(
    monkeypatch: pytest.MonkeyPatch, closes: bool
) -> None:
    WatchedFuture.timed_out = threading.Event()
    monkeypatch.setattr(supervisor_module, "_WRITE_CHECK", 0.001)
    shim = SimpleNamespace(futures=SimpleNamespace(Future=WatchedFuture))
    monkeypatch.setattr(supervisor_module, "concurrent", shim)
    supervisor = make(FakeApi(), coding)

    async def scenario() -> None:
        sends = sends_of(supervisor)
        held = HeldLoop(asyncio.get_running_loop())
        sends._loop = held  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
        worker = threading.Thread(target=sends.note, args=("late",))
        worker.start()
        await asyncio.to_thread(WatchedFuture.timed_out.wait, 5.0)
        assert WatchedFuture.timed_out.is_set()  # the loop did not write it in time
        if closes:
            sends.close()
            await asyncio.to_thread(worker.join, 5.0)  # it stops waiting at once
            assert not worker.is_alive()
            held.release()  # a closed `_Sends` drops the held write
        else:
            held.release()
            await asyncio.to_thread(worker.join, 5.0)
            assert not worker.is_alive()
        await asyncio.sleep(0)  # the held write runs now
        await asyncio.sleep(0)

    asyncio.run(scenario())

    texts = [message.body["text"] for message in supervisor.outbox.pending()]
    assert texts == ([] if closes else ["late"])


# ── a start that is refused ─────────────────────────────────────────────────

CHECKSUM = "sha256:" + "b" * 64
ROUTINE = (
    "from stateous import StateRef, on_state\n\n"
    "CODING = StateRef('coding')\n\n\n"
    "@on_state(CODING)\ndef coding(ctx: object) -> None:\n    return None\n"
)


def folder_at(path: Path) -> Folder:
    path.mkdir()
    (path / "coding.py").write_text(ROUTINE, encoding="utf-8")
    config = FolderConfig(url="http://fake.test", tasks=2, name="box-1")
    return Folder(path.resolve(), config, CHECKSUM)


def test_an_outbox_that_cannot_be_made_is_refused_in_one_sentence(tmp_path: Path) -> None:
    folder = folder_at(tmp_path / "w1")
    (folder.path / ".stateous").write_text("a file, not a folder", encoding="utf-8")

    with pytest.raises(SupervisorError) as refused:
        asyncio.run(open_supervisor(folder, FakeApi(checksum=CHECKSUM)))

    assert str(refused.value).startswith(f"cannot make {folder.path / '.stateous'}: ")


def test_a_control_plane_that_refuses_the_health_check_is_refused_in_one_sentence(
    tmp_path: Path,
) -> None:
    class NotOursApi(FakeApi):
        async def health(self) -> dict[str, Any]:
            raise Refused(200, "the control plane's answer is not JSON")

    folder = folder_at(tmp_path / "w2")

    with pytest.raises(SupervisorError) as refused:
        asyncio.run(open_supervisor(folder, NotOursApi()))

    assert str(refused.value) == (
        "http://fake.test did not answer as a Stateous control plane: "
        "the control plane's answer is not JSON"
    )
