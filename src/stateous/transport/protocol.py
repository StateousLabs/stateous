"""`Transport`: how a supervisor reaches a control plane, one attempt for each call."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any, Protocol

from stateous.model.wire import Outcome, TaskGrant


class Transport(Protocol):
    """The six calls of a supervisor, over any wire. Each method makes one attempt. It
    returns the result, or raises `Refused` (the control plane said no) or
    `NotReached` (no answer, or "try again"). The supervisor tries again by itself."""

    where: str
    """Where the control plane is, for log lines: a URL, a queue's name, "in process"."""

    async def health(self) -> dict[str, Any]:
        """`{"workflow", "checksum"}` of the graph that the control plane serves."""
        ...

    async def claim(
        self,
        supervisor: str,
        states: Collection[str],
        *,
        request: str | None = None,
        wait: float = 0,
        labels: Mapping[str, str] | None = None,
    ) -> TaskGrant | None:
        """A task of one of `states`, or None. With `wait`: the control plane may hold
        the call up to `wait` seconds for a task. With `labels`: only a task whose token
        has each of them. The same `request` gets the same grant again."""
        ...

    async def report(self, task: int, outcome: Outcome, *, supervisor: str) -> None:
        """The outcome of a task. The outcome names its attempt."""
        ...

    async def heartbeat(self, supervisor: str, tasks: Collection[int]) -> list[int]:
        """The ids among `tasks` that the supervisor no longer holds."""
        ...

    async def note(self, task: int, text: str, *, attempt: int) -> None:
        """One line of progress, for that attempt of the task."""
        ...

    async def set_memory(self, task: int, key: str, value: Any, *, attempt: int) -> None:
        """Save one key of the routine's memory. `None` deletes the key."""
        ...

    async def aclose(self) -> None:
        """Let go of the connections."""
        ...
