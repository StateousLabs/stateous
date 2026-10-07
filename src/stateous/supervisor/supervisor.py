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
A claim that waits to try again gives up. The second signal (or `stop_now`)
cancels the routines and the claim, and `run` returns 1.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import signal
import sys
import threading
import uuid
from collections.abc import Callable, Collection, Generator
from dataclasses import dataclass, replace
from types import FrameType
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel

from stateous.model.context import Context
from stateous.model.contract import StateRef
from stateous.model.events import Occurrence
from stateous.model.wire import Outcome, TaskGrant
from stateous.supervisor.folder import CONTRACT, Folder
from stateous.supervisor.registry import Registry, Routine
from stateous.supervisor.remote import Refused

log = logging.getLogger("stateous.supervisor")

HEARTBEAT_EVERY = 30.0
"""Seconds between heartbeats. The control plane's lease is 2 minutes."""

IDLE = 1.0
"""Seconds to wait after a claim that got no task."""

CANCEL_WAIT = 5.0
"""After the second signal: seconds to wait for the cancelled routines to end."""


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

    async def report(self, task: int, outcome: Outcome, *, supervisor: str) -> None: ...

    async def heartbeat(self, supervisor: str, tasks: Collection[int]) -> list[int]: ...

    async def note(self, task: int, text: str, *, attempt: int) -> None: ...

    async def set_memory(self, task: int, key: str, value: Any, *, attempt: int) -> None: ...


@dataclass(frozen=True)
class _Send:
    kind: Literal["memory", "note"]
    key: str
    """The memory key, or the note's text."""
    value: Any = None


class _Sends:
    """One task's memory writes and notes, sent to the control plane in order.

    The routine adds them from the loop (an async routine) or from its thread (a
    def routine). A send from the thread goes to the loop with
    `call_soon_threadsafe`. The thread's result goes to the loop the same way and
    later, so its sends are in the queue before the result arrives."""

    def __init__(self, api: Api, task: int, attempt: int) -> None:
        self._api = api
        self._task = task
        self._attempt = attempt
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._queue: asyncio.Queue[_Send] = asyncio.Queue()
        self._closed = False

    def memory(self, key: str, value: Any) -> None:
        self._add(_Send("memory", key, value))

    def note(self, text: str) -> None:
        self._add(_Send("note", text))

    def close(self) -> None:
        """Drop all later sends: the task is over for this supervisor."""
        self._closed = True

    async def deliver(self) -> None:
        """Send the queued items one at a time, until cancelled."""
        while True:
            send = await self._queue.get()
            try:
                if send.kind == "memory":
                    await self._api.set_memory(
                        self._task, send.key, send.value, attempt=self._attempt
                    )
                else:
                    await self._api.note(self._task, send.key, attempt=self._attempt)
            except Refused as error:
                log.warning(
                    "task %d: the control plane refused a %s: %s",
                    self._task,
                    send.kind,
                    error.message,
                )
            except Exception:
                log.exception("task %d: could not send a %s", self._task, send.kind)
            finally:
                self._queue.task_done()

    async def flush(self) -> None:
        """Wait until every queued item is sent."""
        await self._queue.join()

    def _add(self, send: _Send) -> None:
        if self._closed:
            return
        if threading.get_ident() == self._loop_thread:
            self._queue.put_nowait(send)
            return
        with contextlib.suppress(RuntimeError):  # the loop has closed
            self._loop.call_soon_threadsafe(self._put, send)

    def _put(self, send: _Send) -> None:
        if not self._closed:
            self._queue.put_nowait(send)


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
    ) -> None:
        if tasks < 1:
            raise ValueError("tasks must be 1 or more")
        self.api = api
        self.registry = registry
        self.name = name
        self.tasks = tasks
        self.heartbeat_every = heartbeat_every
        self.idle = idle
        self.cancel_wait = cancel_wait
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

    async def _claim_forever(self) -> None:
        """Claim tasks until the stop. The stop does not cut a claim request in flight.

        Each claim has a random request id. A claim that failed with no answer is
        sent again with the same id: if the control plane granted a task but its
        answer was lost, the claim gets that task again. After an answer (a task,
        no task, or a refusal), the next claim gets a new id."""
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
            self._do(running, _Sends(self.api, grant.task, grant.attempt)),
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
            await self._report(grant, outcome)
        except asyncio.CancelledError:
            raise
        except Exception:  # a fault of the supervisor; the task fails when its lease ends
            log.exception("task %d of %s: could not finish", grant.task, grant.token)
        finally:
            sends.close()
            delivering.cancel()
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

    async def _report(self, grant: TaskGrant, outcome: Outcome) -> None:
        """Report the outcome for the grant's attempt: a late report of an old
        attempt cannot land on a new one."""
        outcome = replace(outcome, attempt=grant.attempt)
        try:
            await self.api.report(grant.task, outcome, supervisor=self.name)
        except Refused as error:
            log.warning(
                "task %d of %s: the control plane refused the report: %s",
                grant.task,
                grant.token,
                error.message,
            )
            if outcome.kind != "failed" and await self._still_held(grant, error):
                await self._report_refusal(grant, error)
            return
        except Exception:
            log.exception("task %d of %s: could not report", grant.task, grant.token)
            return
        said = outcome.event if outcome.kind == "event" else outcome.kind
        log.info("task %d of %s: reported %s", grant.task, grant.token, said)

    async def _still_held(self, grant: TaskGrant, refusal: Refused) -> bool:
        """After a refused report: is the task still this supervisor's, on this
        attempt? 404: the task is gone. 409 that names the attempt: the task moved
        to a new attempt, so this one is over, even if the task id is still this
        supervisor's. 409 for another reason: the task is not ours, or the event is
        not allowed. The answer has no code, so a heartbeat tells the rest."""
        if refusal.status == 404:
            return False
        if refusal.status != 409:
            return True
        if _names_another_attempt(refusal.message):
            return False
        try:
            return grant.task not in await self.api.heartbeat(self.name, [grant.task])
        except Exception:
            log.exception("task %d of %s: could not check the task", grant.task, grant.token)
            return False

    async def _report_refusal(self, grant: TaskGrant, refusal: Refused) -> None:
        """Fail the task with the refusal, once, so it does not wait for its lease."""
        failed = Outcome(
            kind="failed",
            error=f"the control plane refused the report: {refusal.message}",
            attempt=grant.attempt,
        )
        try:
            await self.api.report(grant.task, failed, supervisor=self.name)
        except Refused as error:
            log.warning(
                "task %d of %s: the control plane refused the failure report too: %s",
                grant.task,
                grant.token,
                error.message,
            )
            return
        except Exception:
            log.exception("task %d of %s: could not report", grant.task, grant.token)
            return
        log.info("task %d of %s: reported failed", grant.task, grant.token)


def _names_another_attempt(message: str) -> bool:
    """True for a 409 refusal that names the task's attempt (see `TaskNotHeld` in
    `stateous.control.engine`): "task 7 is at attempt 2, not attempt 3". Other 409
    refusals do not say "is at attempt"."""
    return "is at attempt" in message


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
    must have routines. An unreachable control plane is tried again with backoff."""
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
