"""The supervisor: it claims tasks of the states its routines serve, runs the
routines, and reports what they returned.

One asyncio loop does all the work. An `async def` routine runs as a task on the
loop. A `def` routine runs in a daemon thread: a thread cannot be stopped, and a
daemon thread does not keep the process alive after the second stop signal. At
most `tasks` routines run at once.

A routine's `ctx.memory[...] = value` and `ctx.note(...)` go to the control plane
at once, in order, for each task. The report waits until they are sent. It
carries the routine's context writes and its timers (`ctx.schedule`). The
report, each note and each memory write name the attempt of the grant, so the
control plane refuses them when the task is on a new attempt.

Each report, note and memory write goes to the outbox first, then to the control
plane (`stateous.supervisor.outbox`). It leaves the outbox when the control plane
answers it for good: it accepts it, or it refuses it. When the control plane
cannot be reached, the message stays, and the later messages of that task stay
after it. At start, before the first claim, the supervisor sends the messages
left in the outbox, in order.

Every `heartbeat_every` seconds, the supervisor names the tasks it still works
on. The control plane answers with the tasks it took back: the token left the
state, the token was cancelled, or the lease ended. The supervisor cancels their
async routines. It cannot stop a thread routine, so it drops that routine's
result.

A report that the control plane refuses for a reason other than "the task is not
yours" (for example, the context writes break the schema) is sent again once, as
a failure that gives the refusal. So the task fails at once, not when its lease
ends.

The first stop signal stops the claims and waits for the running routines. A
claim request in flight is not cut: when it gets a task, that routine runs too.
A claim that waits to try again gives up. After the first signal, a message has
one try. When the control plane cannot be reached, the message stays in the
outbox for the next start: the stop does not wait for the control plane. The
second signal (or `stop_now`) cancels the routines and the claim, and `run`
returns 1.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import inspect
import logging
import signal
import sqlite3
import sys
import threading
import uuid
from collections.abc import Callable, Collection, Generator
from dataclasses import dataclass, replace
from types import FrameType
from typing import Any, Protocol, cast

from pydantic import BaseModel

from stateous.model.context import Context
from stateous.model.contract import StateRef
from stateous.model.events import Occurrence
from stateous.model.wire import Outcome, TaskGrant
from stateous.supervisor.folder import CONTRACT, Folder
from stateous.supervisor.outbox import (
    Kind,
    Message,
    Outbox,
    OutboxBusy,
    OutboxError,
    OutboxWriteError,
    outbox_path,
)
from stateous.supervisor.registry import Registry, Routine
from stateous.supervisor.remote import GaveUp, Refused

log = logging.getLogger("stateous.supervisor")

HEARTBEAT_EVERY = 30.0
"""Seconds between heartbeats. The control plane's lease is 2 minutes."""

IDLE = 1.0
"""Seconds to wait after a claim that got no task."""

CANCEL_WAIT = 5.0
"""After the second signal: seconds to wait for the cancelled routines to end."""

_WRITE_CHECK = 0.1
"""Seconds between the checks of a def routine that waits for an outbox write."""


class SupervisorError(Exception):
    """The supervisor cannot start. The message is one plain sentence."""


class Api(Protocol):
    """What the supervisor needs from the control plane. `ControlPlaneApi` is the
    real one; tests use a fake."""

    url: str

    async def health(self) -> dict[str, Any]: ...

    async def claim(
        self,
        supervisor: str,
        states: Collection[str],
        *,
        stop: asyncio.Event | None = None,
        request: str | None = None,
    ) -> TaskGrant | None: ...

    async def report(
        self, task: int, outcome: Outcome, *, supervisor: str, stop: asyncio.Event | None = None
    ) -> None: ...

    async def heartbeat(
        self, supervisor: str, tasks: Collection[int], *, stop: asyncio.Event | None = None
    ) -> list[int]: ...

    async def note(
        self, task: int, text: str, *, attempt: int, stop: asyncio.Event | None = None
    ) -> None: ...

    async def set_memory(
        self,
        task: int,
        key: str,
        value: Any,
        *,
        attempt: int,
        stop: asyncio.Event | None = None,
    ) -> None: ...


class _Sends:
    """One grant's memory writes, notes and report: each is written to the outbox,
    then sent to the control plane, in order.

    The routine adds them from the loop (an async routine) or from its thread (a
    def routine). A send from the thread goes to the loop with
    `call_soon_threadsafe`, and the thread waits until the loop wrote it to the
    outbox. The thread's result goes to the loop the same way and later, so its
    sends are in the queue before the result arrives. The outbox is written on the
    loop only.

    When the outbox cannot be written (for example, the disk is full), the routine
    gets `OutboxWriteError` from `ctx.note()` or `ctx.memory[...] = ...`. An async
    routine and a def routine get it the same way."""

    def __init__(self, supervisor: Supervisor, grant: TaskGrant) -> None:
        self._supervisor = supervisor
        self._grant = grant
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._queue: asyncio.Queue[Message] = asyncio.Queue()
        self._closed = False
        self._held = False
        """A message did not reach the control plane. The later messages stay in the
        outbox after it, so their order is kept."""

    def memory(self, key: str, value: Any) -> None:
        self._add("memory", {"key": key, "value": value})

    def note(self, text: str) -> None:
        self._add("note", {"text": text})

    def report(self, outcome: Outcome) -> None:
        """Add the report. Call it on the loop."""
        self._put("report", outcome.to_json())

    def close(self) -> None:
        """Drop all later sends: the task is over for this supervisor."""
        self._closed = True

    async def deliver(self) -> None:
        """Send the queued messages one at a time, until cancelled."""
        while True:
            message = await self._queue.get()
            try:
                if not self._held:
                    self._held = not await self._supervisor.send(message)
            finally:
                self._queue.task_done()

    async def flush(self) -> None:
        """Wait until each queued message is sent, or stays in the outbox."""
        await self._queue.join()

    def _add(self, kind: Kind, body: dict[str, Any]) -> None:
        if self._closed:
            return
        if threading.get_ident() == self._loop_thread:
            self._put(kind, body)
            return
        written: concurrent.futures.Future[None] = concurrent.futures.Future()

        def put() -> None:
            try:
                self._put(kind, body)
            except Exception as error:
                written.set_exception(error)
            else:
                written.set_result(None)

        try:
            self._loop.call_soon_threadsafe(put)
        except RuntimeError:  # the loop has closed
            return
        while not self._closed:  # after the close, the loop does not write it
            try:
                written.result(timeout=_WRITE_CHECK)  # raises the error of the write
            except TimeoutError:
                continue
            return

    def _put(self, kind: Kind, body: dict[str, Any]) -> None:
        """Write the message to the outbox, and queue it. Raise `OutboxWriteError`
        when the outbox cannot be written."""
        if self._closed:
            return
        grant = self._grant
        try:
            message = self._supervisor.outbox.append(
                grant.task, grant.attempt, grant.token, kind, body
            )
        except sqlite3.Error as error:
            problem = f"cannot write the {kind} to the outbox: {error}"
            log.error("task %d of %s: %s", grant.task, grant.token, problem)
            raise OutboxWriteError(problem) from None
        self._queue.put_nowait(message)


@dataclass(eq=False)
class _Running:
    """One routine of one grant. The control plane can grant the same task id again
    while an old routine of it still ends, so two of them can have the same id."""

    grant: TaskGrant
    job: asyncio.Task[None] | None = None
    lost: bool = False
    """The control plane took the task back."""
    reporting: bool = False
    """The routine is done, and its report is on its way."""


class Supervisor:
    """Claims tasks, runs routines, sends heartbeats and reports, and stops on signals."""

    def __init__(
        self,
        api: Api,
        registry: Registry,
        *,
        name: str,
        tasks: int,
        heartbeat_every: float = HEARTBEAT_EVERY,
        idle: float = IDLE,
        cancel_wait: float = CANCEL_WAIT,
        outbox: Outbox | None = None,
    ) -> None:
        """`outbox`: the outbox of the folder. None: an outbox in memory (for tests)."""
        if tasks < 1:
            raise ValueError("tasks must be 1 or more")
        self.api = api
        self.registry = registry
        self.name = name
        self.tasks = tasks
        self.heartbeat_every = heartbeat_every
        self.idle = idle
        self.cancel_wait = cancel_wait
        self.outbox = outbox if outbox is not None else Outbox.in_memory()
        self._running: set[_Running] = set()
        self._stopping = asyncio.Event()
        self._forced = asyncio.Event()
        self._freed = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._signals = 0
        self._early: list[Callable[[], None]] = []

    @property
    def running(self) -> list[int]:
        """The tasks whose routines run now, or whose reports are on their way."""
        return sorted({running.grant.task for running in self._running})

    async def run(self) -> int:
        """Claim and run until a stop signal. Returns 0 after a graceful stop (the
        running routines ended and were reported), 1 after the second signal."""
        self._loop = asyncio.get_running_loop()
        for early in self._early:
            early()
        beating = asyncio.create_task(self._beat_forever(), name="stateous-heartbeat")
        claiming = asyncio.create_task(self._claim_forever(), name="stateous-claim")
        try:
            await self._stopping.wait()
            await self._end_claims(claiming)
            await self._wait_for_routines()
        finally:
            beating.cancel()
            claiming.cancel()
            await asyncio.gather(beating, claiming, return_exceptions=True)
        return 1 if self._forced.is_set() else 0

    def on_signal(self) -> None:
        """A stop signal. Safe to call from a signal handler or from another thread."""
        self._soon(self._signalled)

    def stop_now(self) -> None:
        """Stop at once, as after a second signal: cancel the routines, and `run`
        returns 1. Safe to call from a signal handler or from another thread."""
        self._soon(self._force)

    def _soon(self, call: Callable[[], None]) -> None:
        loop = self._loop
        if loop is None:
            self._early.append(call)
            return
        with contextlib.suppress(RuntimeError):  # the loop has closed: nothing is left to stop
            loop.call_soon_threadsafe(call)

    def _signalled(self) -> None:
        self._signals += 1
        if self._signals == 1 and not self._stopping.is_set():
            log.warning(
                "stopping: no new tasks; waiting for %d routine(s) (signal again to stop now)",
                len(self._running),
            )
            self._stopping.set()
            self._freed.set()  # a claim loop that rests sees the stop now
        elif self._signals >= 2:
            self._force()

    def _force(self) -> None:
        if self._forced.is_set():
            return
        log.warning("stopping now: cancelling %d routine(s)", len(self._running))
        self._forced.set()
        self._stopping.set()
        self._freed.set()
        for running in self._running:
            if running.job is not None:
                running.job.cancel()

    async def _end_claims(self, claiming: asyncio.Task[None]) -> None:
        """After the first signal: let a claim request in flight finish (its task
        runs), unless the stop is forced first."""
        forced = asyncio.ensure_future(self._forced.wait())
        try:
            await asyncio.wait({claiming, forced}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            forced.cancel()
        if not claiming.done():
            claiming.cancel()
        await asyncio.gather(claiming, return_exceptions=True)

    async def _wait_for_routines(self) -> None:
        idle = asyncio.ensure_future(self._idle.wait())
        forced = asyncio.ensure_future(self._forced.wait())
        try:
            await asyncio.wait({idle, forced}, return_when=asyncio.FIRST_COMPLETED)
            if not idle.done():
                await asyncio.wait({idle}, timeout=self.cancel_wait)
            if not idle.done():
                log.warning("%d routine(s) did not stop; exiting without them", len(self._running))
        finally:
            idle.cancel()
            forced.cancel()

    def close(self) -> None:
        """Close the outbox, and release its lock. Call it after `run`."""
        self.outbox.close()

    async def _claim_forever(self) -> None:
        """Send the messages left in the outbox. Then claim tasks until the stop. The
        stop does not cut a claim request in flight.

        Each claim has a random request id. A claim that failed with no answer is
        sent again with the same id: if the control plane granted a task but its
        answer was lost, the claim gets that task again. After an answer (a task,
        no task, or a refusal), the next claim gets a new id."""
        await self._send_left()
        served = self.registry.served
        request = uuid.uuid4().hex
        while not self._stopping.is_set():
            if len(self._running) >= self.tasks:
                await self._rest(None)
                continue
            try:
                grant = await self.api.claim(
                    self.name, served, stop=self._stopping, request=request
                )
            except Refused as error:
                log.warning("the control plane refused a claim: %s", error.message)
                grant = None
                request = uuid.uuid4().hex
            except Exception:
                log.exception("a claim failed")
                grant = None
            else:
                request = uuid.uuid4().hex
            if grant is None:
                await self._rest(self.idle)
            else:
                self._start(grant)

    async def _rest(self, seconds: float | None) -> None:
        """Wait `seconds` (None: no limit), or less when a routine ends or a stop comes."""
        self._freed.clear()
        if self._stopping.is_set():
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._freed.wait(), seconds)

    async def _beat_forever(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_every)
            held = [running for running in self._running if not running.lost]
            if not held:
                continue
            try:
                lost = set(
                    await self.api.heartbeat(self.name, sorted({r.grant.task for r in held}))
                )
            except Refused as error:
                log.warning("the control plane refused a heartbeat: %s", error.message)
                continue
            except Exception:
                log.exception("a heartbeat failed")
                continue
            for running in held:  # a routine that started since then is not lost
                if running.grant.task in lost:
                    self._lose(running)

    def _lose(self, running: _Running) -> None:
        if running.lost or running.reporting:
            return
        running.lost = True
        log.warning(
            "task %d of %s: the control plane took it back; cancelling its routine",
            running.grant.task,
            running.grant.token,
        )
        if running.job is not None:
            running.job.cancel()

    def _start(self, grant: TaskGrant) -> None:
        if self._forced.is_set():
            log.warning(
                "task %d of %s: stopping now, so its routine does not run",
                grant.task,
                grant.token,
            )
            return
        for old in list(self._running):
            if old.grant.task == grant.task:  # the control plane took it back already
                self._lose(old)
        log.info(
            "task %d: %s of %s, attempt %d", grant.task, grant.state, grant.token, grant.attempt
        )
        running = _Running(grant)
        running.job = asyncio.create_task(
            self._do(running, _Sends(self, grant)),
            name=f"stateous-task-{grant.task}",
        )
        self._running.add(running)
        self._idle.clear()

    async def _do(self, running: _Running, sends: _Sends) -> None:
        grant = running.grant
        delivering = asyncio.create_task(sends.deliver())
        try:
            outcome = await self._call(grant, sends)
            await sends.flush()
            if running.lost or self._forced.is_set():
                return
            running.reporting = True
            sends.report(replace(outcome, attempt=grant.attempt))
            await sends.flush()
        except asyncio.CancelledError:
            raise
        except OutboxWriteError:  # `_put` logged it; the task fails when its lease ends
            pass
        except Exception:  # a fault of the supervisor; the task fails when its lease ends
            log.exception("task %d of %s: could not finish", grant.task, grant.token)
        finally:
            sends.close()
            delivering.cancel()
            if running.lost:  # the control plane refuses the messages of this attempt now
                self.outbox.drop(grant.task, grant.attempt)
            self._running.discard(running)
            self._freed.set()
            if not self._running:
                self._idle.set()

    async def _call(self, grant: TaskGrant, sends: _Sends) -> Outcome:
        routine = self.registry.routines.get(grant.state)
        state = self.registry.states.get(grant.state)
        if routine is None or state is None:
            return Outcome(
                kind="failed", error=f"this supervisor has no routine for {grant.state!r}"
            )
        try:
            ctx = Context[Any](grant, save_memory=sends.memory, note=sends.note)
            if inspect.iscoroutinefunction(routine):
                value = await routine(ctx)
            else:
                value = await _in_thread(routine, ctx)
            return outcome_of(state, value, ctx)
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # also SystemExit and KeyboardInterrupt
            return Outcome(kind="failed", error=f"{type(error).__name__}: {error}")

    async def _send_left(self) -> None:
        """Send the messages that an earlier run left in the outbox, in order. A
        message that does not reach the control plane stays, with the later
        messages of its task. After a stop, the rest stays for the next start."""
        left = self.outbox.pending()
        if not left:
            return
        log.info("sending %d message(s) left in the outbox", len(left))
        held: set[int] = set()
        for message in left:
            if message.task in held:
                continue
            if not await self.send(message):
                held.add(message.task)
                if self._stopping.is_set():
                    return

    async def send(self, message: Message) -> bool:
        """Send one message of the outbox. Remove it when the control plane answered
        it for good. Returns False when it stays in the outbox: the control plane
        could not be reached before the stop."""
        try:
            if message.kind == "report":
                return await self._send_report(message)
            if message.kind == "memory":
                key, value = message.body["key"], message.body["value"]
                await self.api.set_memory(
                    message.task, key, value, attempt=message.attempt, stop=self._stopping
                )
            else:
                text = message.body["text"]
                await self.api.note(
                    message.task, text, attempt=message.attempt, stop=self._stopping
                )
        except GaveUp:
            log.warning(
                "task %d of %s: the control plane cannot be reached; "
                "the %s stays in the outbox for the next start",
                message.task,
                message.token,
                message.kind,
            )
            return False
        except Refused as error:
            log.warning(
                "task %d: the control plane refused a %s: %s",
                message.task,
                message.kind,
                error.message,
            )
        except Exception:  # sent again, it fails again
            log.exception("task %d: could not send a %s", message.task, message.kind)
        self.outbox.remove(message.id)
        return True

    async def _send_report(self, message: Message) -> bool:
        """Send a report of the outbox. A refusal of an event or of waiting can add
        a failure report to the outbox and send it (`_report_refusal`). Returns as
        `send`."""
        task, token = message.task, message.token
        outcome = replace(Outcome.from_json(message.body), attempt=message.attempt)
        try:
            await self.api.report(task, outcome, supervisor=self.name, stop=self._stopping)
        except Refused as error:
            again = outcome.kind == "failed" and _is_refusal(outcome)
            log.warning(
                "task %d of %s: the control plane refused %s: %s",
                task,
                token,
                "the failure report too" if again else "the report",
                error.message,
            )
            if outcome.kind != "failed" and await self._still_held(task, token, error):
                return await self._report_refusal(message, error)
        except GaveUp:
            raise
        except Exception:
            log.exception("task %d of %s: could not report", task, token)
        else:
            said = outcome.event if outcome.kind == "event" else outcome.kind
            log.info("task %d of %s: reported %s", task, token, said)
        self.outbox.remove(message.id)
        return True

    async def _still_held(self, task: int, token: str, refusal: Refused) -> bool:
        """After a refused report: is the task still this supervisor's, on this
        attempt? The refusal's code tells (see the guide, "The HTTP API"), never
        its sentence.
        `task_not_found` (or another 404): the task is gone. `attempt_over`: the
        task moved to a new attempt, so this one is over, even if the task id is
        still this supervisor's. Another 409: the task is not ours, or the event is
        not allowed; a heartbeat tells which.

        When the heartbeat does not reach the control plane before the stop, it
        raises `GaveUp`: nothing is decided, and the report stays in the outbox."""
        if refusal.code in ("task_not_found", "attempt_over") or refusal.status == 404:
            return False
        if refusal.status != 409:
            return True
        try:
            return task not in await self.api.heartbeat(self.name, [task], stop=self._stopping)
        except GaveUp:
            raise
        except Exception:
            log.exception("task %d of %s: could not check the task", task, token)
            return False

    async def _report_refusal(self, refused: Message, refusal: Refused) -> bool:
        """Fail the task with the refusal, once, so it does not wait for its lease.
        The failure report goes to the outbox before the refused report leaves it.
        Returns as `send`."""
        failed = Outcome(kind="failed", error=REFUSAL + refusal.message, attempt=refused.attempt)
        message = self.outbox.append(
            refused.task, refused.attempt, refused.token, "report", failed.to_json()
        )
        self.outbox.remove(refused.id)
        return await self.send(message)


REFUSAL = "the control plane refused the report: "
"""The start of the error of a failure report that a refused report causes."""


def _is_refusal(outcome: Outcome) -> bool:
    """True for a failure report that a refused report caused."""
    return outcome.error is not None and outcome.error.startswith(REFUSAL)


def outcome_of(state: StateRef[Any], value: object, ctx: Context[Any]) -> Outcome:
    """The report for what a routine of `state` returned: an event, waiting, or a
    failure that says what is wrong. The context writes are JSON already: a write
    that is not JSON raises in the routine (`Context.__setitem__`)."""
    writes = ctx.writes()
    schedules = tuple(ctx.schedules())
    if value is None:
        return Outcome(kind="waiting", context=writes, schedules=schedules)
    if isinstance(value, Occurrence):
        occurrence = cast("Occurrence[Any]", value)
        if not state.may_emit(occurrence):
            allowed = ", ".join(event.name for event in state.emits) or "nothing"
            return Outcome(
                kind="failed",
                error=(
                    f"the routine returned {occurrence.name!r}, which {state.name!r} "
                    f"may not emit (allowed: {allowed})"
                ),
            )
        payload = occurrence.payload
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else None
        return Outcome(
            kind="event",
            event=occurrence.name,
            payload=data,
            context=writes,
            schedules=schedules,
        )
    shown = repr(value)
    if len(shown) > 100:
        shown = shown[:97] + "..."
    return Outcome(
        kind="failed",
        error=f"the routine of {state.name!r} returned {shown}; return an event or None",
    )


async def _in_thread(routine: Routine, ctx: Context[Any]) -> Any:
    """Call a def routine in a daemon thread, and wait for its result. When the
    waiting task is cancelled, the thread runs on and its result is dropped."""
    loop = asyncio.get_running_loop()
    result: asyncio.Future[Any] = loop.create_future()

    def settle(value: Any, error: Exception | None) -> None:
        if result.done():
            return  # the task was cancelled: the result is dropped
        if error is None:
            result.set_result(value)
        else:
            result.set_exception(error)

    def work() -> None:
        outcome: tuple[Any, Exception | None]
        try:
            outcome = (routine(ctx), None)
        except Exception as error:
            outcome = (None, error)
        except BaseException as error:
            outcome = (None, RuntimeError(f"{type(error).__name__}: {error}"))
        with contextlib.suppress(RuntimeError):  # the loop has closed
            loop.call_soon_threadsafe(settle, *outcome)

    threading.Thread(target=work, name=f"stateous-routine-{ctx.task}", daemon=True).start()
    return await result


async def open_supervisor(
    folder: Folder,
    api: Api,
    *,
    heartbeat_every: float = HEARTBEAT_EVERY,
    idle: float = IDLE,
) -> Supervisor:
    """The supervisor of `folder`, checked against the control plane: its
    `contract.py` must be for the graph the control plane serves, and the folder
    must have routines. An unreachable control plane is tried again with backoff.

    It opens the folder's outbox first. Another supervisor on the folder holds the
    outbox: then this one does not start."""
    path = outbox_path(folder.path)
    try:
        outbox = Outbox.open(path)
    except OutboxBusy:
        raise SupervisorError(
            f"another supervisor runs on {folder.path} now: stop it, or use another folder"
        ) from None
    except OutboxError as error:
        raise SupervisorError(str(error)) from None
    try:
        return await _checked(folder, api, outbox, heartbeat_every=heartbeat_every, idle=idle)
    except BaseException:
        outbox.close()
        raise


async def _checked(
    folder: Folder, api: Api, outbox: Outbox, *, heartbeat_every: float, idle: float
) -> Supervisor:
    try:
        health = await api.health()
    except Refused as error:
        raise SupervisorError(
            f"{api.url} did not answer as a Stateous control plane: {error.message}"
        ) from None
    served = health.get("checksum")
    if served != folder.checksum:
        raise SupervisorError(
            f"{folder.path / CONTRACT} is for another version of the workflow "
            f"({folder.checksum}; the control plane serves {served}): "
            f"run `stateous init {api.url} {folder.path}` again"
        )
    try:
        registry = Registry.discover(folder.path)
    except Exception as error:
        raise SupervisorError(
            f"cannot load the routines in {folder.path}: {type(error).__name__}: {error}"
        ) from None
    if not registry.served:
        raise SupervisorError(
            f"{folder.path} has no routine: mark a function with @on_state(states.<state>)"
        )
    name = folder.config.supervisor_name()
    log.info("%s serves %s; the control plane is %s", name, ", ".join(registry.served), api.url)
    return Supervisor(
        api,
        registry,
        name=name,
        tasks=folder.config.tasks,
        heartbeat_every=heartbeat_every,
        idle=idle,
        outbox=outbox,
    )


@contextlib.contextmanager
def stop_on_signals(supervisor: Supervisor) -> Generator[None]:
    """While the block runs, SIGINT (Ctrl-C, also on Windows), SIGTERM and, on
    Windows, SIGBREAK (Ctrl-Break) go to `supervisor.on_signal`. The old handlers
    come back after the block. Use it in the main thread only."""

    def handle(signum: int, frame: FrameType | None) -> None:
        supervisor.on_signal()

    signums = [signal.SIGINT, signal.SIGTERM]
    if sys.platform == "win32":
        signums.append(signal.SIGBREAK)
    previous = {signum: signal.signal(signum, handle) for signum in signums}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            if handler is not None:  # None: a handler that Python did not install
                signal.signal(signum, handler)
