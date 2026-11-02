"""`InProcess`: a supervisor and a control plane in the same process. Each call runs
`answer` on a worker thread: no network and no serialization."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Collection

from stateous.control.answer import answer
from stateous.control.plane import ControlPlane
from stateous.control.waiting import WaitingClaims
from stateous.model.wire import TaskGrant
from stateous.transport import Call, CallingTransport, Reply


class InProcess(CallingTransport):
    """The transport to `plane`, in this process. A claim with `wait` waits for "task
    ready" of its states, up to `wait` seconds."""

    def __init__(self, plane: ControlPlane) -> None:
        self.plane = plane
        self._waiting = WaitingClaims()
        self._remove = plane.on_task_ready(self._waiting.ready)

        async def send(call: Call) -> Reply:
            return await asyncio.to_thread(answer, plane, call)

        super().__init__(send, where="in process")

    async def claim(
        self,
        supervisor: str,
        states: Collection[str],
        *,
        request: str | None = None,
        wait: float = 0,
    ) -> TaskGrant | None:
        """A task of one of `states`, or None after up to `wait` seconds."""
        if wait <= 0:
            return await super().claim(supervisor, states, request=request)
        with self._waiting.waiting(states) as ready:
            grant = await super().claim(supervisor, states, request=request)
            if grant is not None:
                return grant
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ready.wait(), wait)
            return await super().claim(supervisor, states, request=request)

    async def aclose(self) -> None:
        """Stop hearing "task ready", and wake each claim that waits."""
        self._remove()
        self._waiting.close()
