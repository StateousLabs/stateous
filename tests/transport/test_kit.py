"""The conformance kit, run on each transport that Stateous ships and on a queue."""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping
from pathlib import Path

import httpx2
import pytest

from stateous import ControlPlane
from stateous.control.api import create_app
from stateous.control.inprocess import InProcess
from stateous.model.wire import TaskGrant
from stateous.supervisor.remote import HttpTransport
from stateous.supervisor.retrying import Retrying
from stateous.testing import check_transport
from stateous.transport import Call, Transport

from ..unit.control.support import TICKET, sdlc, sqlite_url
from .memory_queue import MemoryQueue
from .memory_queue import connect as queue


async def http(plane: ControlPlane) -> Transport:
    client = httpx2.AsyncClient(
        base_url="http://testserver", transport=httpx2.ASGITransport(app=create_app(plane))
    )
    return HttpTransport("http://testserver", client=client)


async def in_process(plane: ControlPlane) -> Transport:
    return InProcess(plane)


def test_the_kit_passes_for_http() -> None:
    asyncio.run(check_transport(http))


def test_the_kit_passes_for_in_process() -> None:
    asyncio.run(check_transport(in_process))


def test_the_kit_passes_for_a_memory_queue() -> None:
    asyncio.run(check_transport(queue))


class NoRequestId(MemoryQueue):
    """A broken transport: it drops the claim's request id."""

    async def claim(
        self,
        supervisor: str,
        states: Collection[str],
        *,
        request: str | None = None,
        wait: float = 0,
        labels: Mapping[str, str] | None = None,
    ) -> TaskGrant | None:
        return await super().claim(supervisor, states, wait=wait, labels=labels)


async def broken(plane: ControlPlane) -> Transport:
    return NoRequestId(plane)


def test_the_kit_names_each_failed_check() -> None:
    with pytest.raises(AssertionError, match="same_request_same_grant"):
        asyncio.run(check_transport(broken))


def test_a_lost_reply_retried_with_the_same_request_gets_the_same_grant(tmp_path: Path) -> None:
    async def main() -> tuple[TaskGrant | None, int]:
        with ControlPlane(sdlc(), database=sqlite_url(tmp_path / "q.db"), migrate=True) as plane:
            plane.start("T-1", TICKET)
            transport = MemoryQueue(plane, drop_replies=1)
            try:

                async def no_wait(seconds: float) -> None:
                    pass

                retrying = Retrying(transport, sleep=no_wait)
                grant = await retrying.claim("box-1", ["coding"], request="r-1")
                return grant, transport.answered
            finally:
                await transport.aclose()

    grant, answered = asyncio.run(main())

    assert grant is not None
    assert grant.attempt == 1  # claimed once: the second answer is the same grant
    assert answered == 2


class NoAttemptOnNotes(MemoryQueue):
    """A broken transport: it drops the attempt of a note."""

    async def note(self, task: int, text: str, *, attempt: int) -> None:
        await self._call(Call("note", {"task": task, "text": text}))


async def drops_attempts(plane: ControlPlane) -> Transport:
    return NoAttemptOnNotes(plane)


def test_the_kit_finds_a_transport_that_drops_the_attempt() -> None:
    with pytest.raises(AssertionError, match="note_keeps_its_attempt"):
        asyncio.run(check_transport(drops_attempts))
