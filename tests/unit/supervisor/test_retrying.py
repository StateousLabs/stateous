"""`Retrying`: the supervisor's retries, over any transport."""

import asyncio
import logging
from collections.abc import Collection
from typing import Any

import pytest

from stateous.model.wire import Outcome, TaskGrant
from stateous.supervisor.retrying import GaveUp, Retrying
from stateous.transport import NotReached, Refused


class Flaky:
    """A transport whose next calls fail as listed, then succeed."""

    where = "a test queue"

    def __init__(self, *failures: Exception) -> None:
        self.failures = list(failures)
        self.calls = 0

    async def _attempt(self) -> None:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)

    async def health(self) -> dict[str, Any]:
        await self._attempt()
        return {"workflow": "sdlc", "checksum": "c"}

    async def claim(
        self,
        supervisor: str,
        states: Collection[str],
        *,
        request: str | None = None,
        wait: float = 0,
    ) -> TaskGrant | None:
        await self._attempt()
        return None

    async def report(self, task: int, outcome: Outcome, *, supervisor: str) -> None:
        await self._attempt()

    async def heartbeat(self, supervisor: str, tasks: Collection[int]) -> list[int]:
        await self._attempt()
        return []

    async def note(self, task: int, text: str, *, attempt: int) -> None:
        await self._attempt()

    async def set_memory(self, task: int, key: str, value: Any, *, attempt: int) -> None:
        await self._attempt()

    async def aclose(self) -> None:
        pass


def retrying(transport: Flaky, delays: list[float]) -> Retrying:
    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    return Retrying(transport, sleep=sleep)


def test_retrying_tries_a_not_reached_call_again_then_succeeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="stateous.supervisor")
    transport = Flaky(NotReached("the broker is down"), NotReached("the broker is down"))
    delays: list[float] = []

    asyncio.run(retrying(transport, delays).note(1, "x", attempt=1))

    assert (transport.calls, delays) == (3, [1.0, 2.0])
    assert caplog.messages == [
        "the broker is down; trying again in 1s",
        "the broker is down; trying again in 2s",
    ]


def test_retrying_gives_up_when_stop_is_set() -> None:
    transport = Flaky(NotReached("down"))
    stop = asyncio.Event()
    stop.set()

    with pytest.raises(GaveUp):
        asyncio.run(retrying(transport, []).note(1, "x", attempt=1, stop=stop))

    assert transport.calls == 1


def test_a_claim_that_gives_up_is_none() -> None:
    stop = asyncio.Event()
    stop.set()

    found = asyncio.run(retrying(Flaky(NotReached("down")), []).claim("b", ["s"], stop=stop))

    assert found is None


def test_a_refusal_is_not_tried_again() -> None:
    transport = Flaky(Refused("task_not_held", "not yours"))
    delays: list[float] = []

    with pytest.raises(Refused, match="not yours"):
        asyncio.run(retrying(transport, delays).heartbeat("b", [1]))

    assert (transport.calls, delays) == (1, [])


def test_retrying_names_where_the_control_plane_is() -> None:
    assert retrying(Flaky(), []).where == "a test queue"
