"""`InProcess`: a supervisor and a control plane in the same process. Each call runs
`answer` on a worker thread: no network and no serialization."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Collection, Mapping

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
        self._waiting = WaitingClaims(plane.on_task_ready)

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
        labels: Mapping[str, str] | None = None,
    ) -> TaskGrant | None:
        """A task of one of `states`, or None after up to `wait` seconds."""
        if wait <= 0:
            return await super().claim(supervisor, states, request=request, labels=labels)
        with self._waiting.waiting(states, labels) as ready:
            grant = await super().claim(supervisor, states, request=request, labels=labels)
            if grant is not None:
                return grant
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ready.wait(), wait)
            return await super().claim(supervisor, states, request=request, labels=labels)

    async def aclose(self) -> None:
        """Wake each claim that waits."""
        self._waiting.close()
