"""`Retrying`: the supervisor's retries, over any `Transport`.

A call that is not reached is tried again after 1, 2, 4, 8, 16, then every 30
seconds, with one log line for each failure, until it gets an answer. A refusal is
not tried again.

A call can get a `stop` event. When it is set, a call that waits to try again
gives up: a claim returns None, and a report, a note, a memory write or a
heartbeat raises `GaveUp`. A call made after the stop has one try. A call in flight
is not cut: the control plane can grant a task in it, and the supervisor must run
that task."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection, Sequence
from typing import Any, TypeVar

from stateous.model.wire import Outcome, TaskGrant
from stateous.transport import NotReached, Transport

log = logging.getLogger("stateous.supervisor")

BACKOFF: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
"""Seconds before each new try. The last value repeats."""

_T = TypeVar("_T")


class GaveUp(Exception):
    """A call did not reach the control plane, and stopped to try again: its `stop`
    event is set."""


class Retrying:
    """The calls of a supervisor over `transport`, each tried until it is answered."""

    def __init__(
        self,
        transport: Transport,
        *,
        backoff: Sequence[float] = BACKOFF,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.transport = transport
        self.where = transport.where
        """Where the control plane is, for log lines."""
        self._backoff = tuple(backoff) or BACKOFF
        self._sleep = sleep

    async def health(self) -> dict[str, Any]:
        """`{"workflow", "checksum"}` of the graph the control plane serves."""
        return await self._until_answered(self.transport.health, None)

    async def claim(
        self,
        supervisor: str,
        states: Collection[str],
        *,
        stop: asyncio.Event | None = None,
        request: str | None = None,
        wait: float = 0,
    ) -> TaskGrant | None:
        """A task of one of `states`, or None. Also None when `stop` is set while the
        claim waits to try again. Each new try sends the same `request` id."""
        try:
            return await self._until_answered(
                lambda: self.transport.claim(supervisor, states, request=request, wait=wait), stop
            )
        except GaveUp:
            return None

    async def report(
        self, task: int, outcome: Outcome, *, supervisor: str, stop: asyncio.Event | None = None
    ) -> None:
        """Report a task. The outcome names the attempt it is for."""
        await self._until_answered(
            lambda: self.transport.report(task, outcome, supervisor=supervisor), stop
        )

    async def heartbeat(
        self, supervisor: str, tasks: Collection[int], *, stop: asyncio.Event | None = None
    ) -> list[int]:
        """The ids among `tasks` that this supervisor no longer holds."""
        return await self._until_answered(lambda: self.transport.heartbeat(supervisor, tasks), stop)

    async def note(
        self, task: int, text: str, *, attempt: int, stop: asyncio.Event | None = None
    ) -> None:
        """One line of progress, for that attempt of the task."""
        await self._until_answered(lambda: self.transport.note(task, text, attempt=attempt), stop)

    async def set_memory(
        self,
        task: int,
        key: str,
        value: Any,
        *,
        attempt: int,
        stop: asyncio.Event | None = None,
    ) -> None:
        """Save one key of the routine's memory. `None` deletes the key."""
        await self._until_answered(
            lambda: self.transport.set_memory(task, key, value, attempt=attempt), stop
        )

    async def aclose(self) -> None:
        await self.transport.aclose()

    async def _until_answered(
        self, attempt: Callable[[], Awaitable[_T]], stop: asyncio.Event | None
    ) -> _T:
        failures = 0
        while True:
            try:
                return await attempt()
            except NotReached as error:
                problem = error.why
            if stop is not None and stop.is_set():
                raise GaveUp
            delay = self._backoff[min(failures, len(self._backoff) - 1)]
            failures += 1
            log.warning("%s; trying again in %gs", problem, delay)
            await self._wait(delay, stop)

    async def _wait(self, delay: float, stop: asyncio.Event | None) -> None:
        """Sleep `delay` seconds. Raise `GaveUp` when `stop` is set first."""
        if stop is None:
            await self._sleep(delay)
            return
        sleeping = asyncio.ensure_future(self._sleep(delay))
        stopping = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({sleeping, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            if not sleeping.done():
                sleeping.cancel()
        if stop.is_set():
            raise GaveUp
        await sleeping
