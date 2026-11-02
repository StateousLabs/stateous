"""`CallingTransport`: a whole transport made of one function that sends a `Call` and
gives back its `Reply`. A queue transport needs only that function."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from typing import Any, cast

from stateous.model.wire import Outcome, TaskGrant, WireError
from stateous.transport.errors import NotReached, Refused
from stateous.transport.messages import RETRY_CODES, Call, Reply


class CallingTransport:
    """A `Transport` over `send`. `send` raises `NotReached` when no reply came."""

    def __init__(self, send: Callable[[Call], Awaitable[Reply]], *, where: str) -> None:
        self._send = send
        self.where = where

    async def health(self) -> dict[str, Any]:
        """`{"workflow", "checksum"}` of the graph that the control plane serves."""
        return _mapping(await self._call(Call("health", {})))

    async def claim(
        self,
        supervisor: str,
        states: Collection[str],
        *,
        request: str | None = None,
        wait: float = 0,
    ) -> TaskGrant | None:
        """A task of one of `states`, or None."""
        args = {"supervisor": supervisor, "states": list(states), "request": request, "wait": wait}
        body = await self._call(Call("claim", args))
        if body is None:
            return None
        try:
            return TaskGrant.from_json(body)
        except WireError as error:
            raise Refused("unexpected", f"the claimed task is not valid: {error}") from None

    async def report(self, task: int, outcome: Outcome, *, supervisor: str) -> None:
        """The outcome of a task."""
        args = {"task": task, "outcome": outcome.to_json(), "supervisor": supervisor}
        await self._call(Call("report", args))

    async def heartbeat(self, supervisor: str, tasks: Collection[int]) -> list[int]:
        """The ids among `tasks` that the supervisor no longer holds."""
        body = _mapping(
            await self._call(Call("heartbeat", {"supervisor": supervisor, "tasks": list(tasks)}))
        )
        lost = body.get("lost")
        if not isinstance(lost, list):
            raise Refused("unexpected", "the heartbeat answer has no list of lost tasks")
        return [int(task) for task in cast("list[Any]", lost)]

    async def note(self, task: int, text: str, *, attempt: int) -> None:
        """One line of progress."""
        await self._call(Call("note", {"task": task, "text": text, "attempt": attempt}))

    async def set_memory(self, task: int, key: str, value: Any, *, attempt: int) -> None:
        """Save one key of the routine's memory."""
        args = {"task": task, "key": key, "value": value, "attempt": attempt}
        await self._call(Call("set_memory", args))

    async def aclose(self) -> None:
        """Nothing to close: `send` owns its wire."""

    async def _call(self, call: Call) -> Any:
        reply = await self._send(call)
        if reply.ok:
            return reply.body
        code = reply.code or "unexpected"
        message = reply.message or "the control plane refused the call"
        if code in RETRY_CODES:
            raise NotReached(f"{code}: {message}")
        raise Refused(code, message)


def _mapping(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise Refused("unexpected", "the control plane's answer is not a JSON object")
    return cast("dict[str, Any]", body)
