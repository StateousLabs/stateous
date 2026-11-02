"""The clock of a control plane: about once a second it renews the hold on the
database and ticks the engine (due timers, timeouts, ended leases)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

log = logging.getLogger("stateous.serve")

TICK_EVERY = 1.0
"""Seconds between two ticks of the clock. `keep_serving` must run at least every
10 seconds; the clock calls it on each tick."""

LOST = (
    "another control plane took this database; this control plane does no more work; "
    "trying again does not help: stop this server"
)
"""The error line of a lost hold, and the answer (503) to each request after it."""


class Ticking(Protocol):
    def tick(self) -> int: ...


class Holding(Protocol):
    def keep_serving(self) -> bool: ...


class Clock:
    """The control plane's clock. Every `every` seconds it
    renews the hold on the database (`Store.keep_serving`) and then calls
    `engine.tick()` (due timers, timeouts, ended leases). Both are synchronous, so
    they run on a thread from the pool. When another control plane took the hold, it sets
    `lost`, logs one error line, calls `on_lost` (if set) once, and ends. A tick that
    fails, or a database that does not answer, is logged; the clock goes on."""

    def __init__(
        self,
        engine: Ticking,
        hold: Holding,
        *,
        every: float = TICK_EVERY,
        on_lost: Callable[[], None] | None = None,
    ) -> None:
        self.engine = engine
        self.hold = hold
        self.every = every
        self.lost = False
        """True when another control plane took the database while this one served."""
        self.on_lost = on_lost
        """Called once, with no arguments, on the server's event loop, after the line
        of a lost hold. It must not block: e.g. it sets `server.should_exit`."""
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        """Tick until `stop()`, or until the hold is lost. A tick in progress ends
        before `run` returns, so the store can close after it."""
        while not self.lost:
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.every)
            except TimeoutError:
                await self.step()
            else:
                return

    async def step(self) -> None:
        """One tick: renew the hold, then tick the engine. A renewal that raises
        skips the engine's tick for this step; it is not a lost hold."""
        kept = await self._kept()
        if kept is None:
            return
        if not kept:
            self.lost = True
            log.error(LOST)
            if self.on_lost is not None:
                self.on_lost()
            return
        try:
            await asyncio.to_thread(self.engine.tick)
        except Exception as error:  # e.g. the database stopped answering: one line
            log.error(
                "the clock's tick failed (%s); it tries again in %s seconds",
                _one_line(error),
                self.every,
            )

    def stop(self) -> None:
        """End `run` after the tick in progress, if there is one."""
        self._stopping.set()

    async def _kept(self) -> bool | None:
        """Whether this control plane still holds the database: True, False (the
        hold is lost), or None when the renewal raised. A database that does not
        answer is not a lost hold: the clock skips this step's tick and tries
        again on the next one."""
        try:
            return await asyncio.to_thread(self.hold.keep_serving)
        except Exception as error:
            log.warning(
                "cannot renew this control plane's hold on the database (%s); "
                "trying again in %s seconds",
                _one_line(error),
                self.every,
            )
            return None


def _one_line(error: BaseException) -> str:
    return " ".join(str(error).split()) or type(error).__name__
