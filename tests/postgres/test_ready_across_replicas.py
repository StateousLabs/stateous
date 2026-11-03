"""'Task ready' across replicas: a task made on one replica wakes a claim on another."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import httpx2
from sqlalchemy import create_engine, text

from stateous import ControlPlane
from stateous.control.api import create_app
from stateous.control.store.store import database_url

from ..conftest import fresh_postgres_database, needs_postgres
from ..unit.control.support import TICKET, sdlc

pytestmark = needs_postgres


def replica(url: str) -> ControlPlane:
    return ControlPlane(sdlc(), database=url, namespace="flow", migrate=True, exclusive=False)


def test_a_task_made_on_one_replica_wakes_a_claim_waiting_on_another() -> None:
    url = fresh_postgres_database()
    first, second = replica(url), replica(url)

    async def main() -> tuple[int, float]:
        app = create_app(second)
        async with httpx2.AsyncClient(
            base_url="http://testserver", transport=httpx2.ASGITransport(app=app)
        ) as client:
            body = {"supervisor": "box", "states": ["coding"], "wait": 5}
            started = time.monotonic()
            claiming = asyncio.create_task(client.post("/tasks/claim", json=body))
            await asyncio.sleep(0.3)
            await asyncio.to_thread(first.start, "T-1", TICKET)
            answer = await claiming
            return answer.status_code, time.monotonic() - started

    try:
        status, took = asyncio.run(main())
    finally:
        first.close()
        second.close()

    assert status == 200
    assert took < 2.5


def test_a_replica_does_not_hear_its_own_ready_twice() -> None:
    url = fresh_postgres_database()
    first, second = replica(url), replica(url)
    heard_first: list[str] = []
    heard_second: list[str] = []

    def into(found: list[str]) -> Any:
        def hear(state: str, labels: Mapping[str, str]) -> None:
            found.append(state)

        return hear

    first.on_task_ready(into(heard_first))
    second.on_task_ready(into(heard_second))
    try:
        first.start("T-1", TICKET)
        deadline = time.monotonic() + 5
        while not heard_second and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)  # time for a second, wrong call
    finally:
        first.close()
        second.close()

    assert heard_first == ["coding"]
    assert heard_second == ["coding"]


def test_a_listener_that_lost_its_connection_hears_again() -> None:
    url = fresh_postgres_database()
    first, second = replica(url), replica(url)
    heard: list[str] = []
    second.on_task_ready(lambda state, labels: heard.append(state))
    admin = create_engine(database_url(url), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            ended = connection.scalar(
                text(
                    "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                    "WHERE query LIKE 'LISTEN%' AND pid <> pg_backend_pid()"
                )
            )
        assert ended
        time.sleep(2.5)  # the listener sees the loss, waits 1 s, and listens again
        first.start("T-1", TICKET)
        deadline = time.monotonic() + 10
        while not heard and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        admin.dispose()
        first.close()
        second.close()

    assert heard == ["coding"]


def test_long_labels_on_replicas_still_work_and_are_heard() -> None:
    url = fresh_postgres_database()
    first, second = replica(url), replica(url)
    labels = {f"k{n}": "é" * 256 for n in range(16)}  # valid; over 8000 bytes as JSON
    heard: list[dict[str, str]] = []
    second.on_task_ready(lambda state, found: heard.append(dict(found)))
    try:
        first.start("T-1", TICKET, labels=labels)
        deadline = time.monotonic() + 5
        while not heard and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        first.close()
        second.close()

    assert heard == [labels]
