"""`WaitingClaims`: claims that wait for a task, woken by "task ready".

The control plane calls `ready(state)` after the commit that makes a task of `state`
claimable, on any thread. Each claim that waits for that state wakes in its own event
loop and claims again. `close()` wakes them all, for a server that stops."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable, Collection, Generator
from contextlib import contextmanager


class WaitingClaims:
    """The claims that wait now, by the states they claim."""

    def __init__(
        self,
        subscribe: Callable[[Callable[[str], None]], Callable[[], None]] | None = None,
    ) -> None:
        """`subscribe` is a control plane's `on_task_ready`. The set hears "ready"
        only while a claim waits, so an app that is made and dropped leaves nothing
        behind."""
        self.subscribe = subscribe
        self._unsubscribe: Callable[[], None] | None = None
        # Reentrant: a signal handler can call `close` on the thread that is inside
        # `waiting` now.
        self._lock = threading.RLock()
        self._waiting: list[tuple[frozenset[str], asyncio.AbstractEventLoop, asyncio.Event]] = []
        self.closed = False
        """True after `close()`: a new wait ends at once."""

    def ready(self, state: str) -> None:
        """A task of `state` can be claimed: wake each claim that waits for it."""
        with self._lock:
            woken = [(loop, event) for states, loop, event in self._waiting if state in states]
        for loop, event in woken:
            _wake(loop, event)

    def close(self) -> None:
        """Wake every claim that waits, and end each later wait at once."""
        with self._lock:
            self.closed = True
            woken = [(loop, event) for _, loop, event in self._waiting]
        for loop, event in woken:
            _wake(loop, event)

    @contextmanager
    def waiting(self, states: Collection[str]) -> Generator[asyncio.Event]:
        """An event that a "ready" of one of `states` sets, while the block runs. Enter
        it before the claim, so a task that comes between the claim and the wait is
        not missed."""
        event = asyncio.Event()
        entry = (frozenset(states), asyncio.get_running_loop(), event)
        with self._lock:
            self._waiting.append(entry)
            if self.subscribe is not None and self._unsubscribe is None:
                self._unsubscribe = self.subscribe(self.ready)
            if self.closed:
                event.set()
        try:
            yield event
        finally:
            with self._lock:
                self._waiting.remove(entry)
                if not self._waiting and self._unsubscribe is not None:
                    self._unsubscribe()
                    self._unsubscribe = None


def _wake(loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
    with contextlib.suppress(RuntimeError):  # a closed loop: no one waits there any more
        loop.call_soon_threadsafe(event.set)
