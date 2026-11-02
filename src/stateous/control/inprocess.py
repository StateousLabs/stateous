"""`InProcess`: a supervisor and a control plane in the same process. Each call runs
`answer` on a worker thread: no network and no serialization."""

from __future__ import annotations

import asyncio

from stateous.control.answer import answer
from stateous.control.plane import ControlPlane
from stateous.transport import Call, CallingTransport, Reply


class InProcess(CallingTransport):
    """The transport to `plane`, in this process."""

    def __init__(self, plane: ControlPlane) -> None:
        self.plane = plane

        async def send(call: Call) -> Reply:
            return await asyncio.to_thread(answer, plane, call)

        super().__init__(send, where="in process")
