"""A transport over two in-memory queues, as a developer's queue transport would be:
the supervisor puts a call on one queue, a consumer answers it with `answer`, and puts
the reply back. It can drop replies, as a broker can."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from stateous import ControlPlane
from stateous.control.answer import answer
from stateous.transport import Call, CallingTransport, NotReached, Reply


class MemoryQueue(CallingTransport):
    """The client side, and the consumer that runs on the control plane's side."""

    def __init__(self, plane: ControlPlane, *, drop_replies: int = 0) -> None:
        self.plane = plane
        self.calls: asyncio.Queue[tuple[dict[str, Any], asyncio.Future[dict[str, Any]]]] = (
            asyncio.Queue()
        )
        self.drop_replies = drop_replies
        self.answered = 0
        self._consumer = asyncio.create_task(self._consume())
        super().__init__(self._send, where="memory queue")

    async def _send(self, call: Call) -> Reply:
        reply: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        await self.calls.put((call.to_json(), reply))  # only JSON crosses the queue
        data = await reply
        if self.drop_replies > 0:
            self.drop_replies -= 1
            raise NotReached("the reply was lost on the way back")
        return Reply.from_json(data)

    async def _consume(self) -> None:
        while True:
            data, reply = await self.calls.get()
            answered = await asyncio.to_thread(answer, self.plane, Call.from_json(data))
            self.answered += 1
            reply.set_result(answered.to_json())

    async def aclose(self) -> None:
        self._consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._consumer


async def connect(plane: ControlPlane) -> MemoryQueue:
    return MemoryQueue(plane)
