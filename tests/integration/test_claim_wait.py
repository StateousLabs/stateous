"""Claims that wait for work: over HTTP, in process, and a supervisor's stop."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx2

from stateous import ControlPlane, supervise
from stateous.cli.serving import GracefulServer, prepare_serve
from stateous.control.api import create_app
from stateous.control.inprocess import InProcess
from stateous.model.wire import TaskGrant

from ..unit.control.support import TICKET, sdlc, sqlite_url
from .supervising import compile_flow, database, server_ready, wait_for
from .test_supervise_api import FLOW, folder_for


def http_client(plane: ControlPlane) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        base_url="http://testserver", transport=httpx2.ASGITransport(app=create_app(plane))
    )


def test_a_waiting_claim_gets_a_task_queued_while_it_waits(tmp_path: Path) -> None:
    async def main() -> tuple[int, float]:
        with ControlPlane(sdlc(), database=sqlite_url(tmp_path / "w.db"), migrate=True) as plane:
            async with http_client(plane) as client:
                started = time.monotonic()
                body = {"supervisor": "box-1", "states": ["coding"], "wait": 5}
                claiming = asyncio.create_task(client.post("/tasks/claim", json=body))
                await asyncio.sleep(0.2)
                await asyncio.to_thread(plane.start, "T-1", TICKET)
                answer = await claiming
                return answer.status_code, time.monotonic() - started

    status, took = asyncio.run(main())

    assert status == 200
    assert took < 2


def test_a_waiting_claim_with_no_task_answers_204_after_wait(tmp_path: Path) -> None:
    async def main() -> tuple[int, float]:
        with ControlPlane(sdlc(), database=sqlite_url(tmp_path / "w.db"), migrate=True) as plane:
            async with http_client(plane) as client:
                started = time.monotonic()
                body = {"supervisor": "box-1", "states": ["coding"], "wait": 0.3}
                answer = await client.post("/tasks/claim", json=body)
                return answer.status_code, time.monotonic() - started

    status, took = asyncio.run(main())

    assert status == 204
    assert 0.25 <= took < 3


def test_a_wait_over_30_is_invalid_argument(tmp_path: Path) -> None:
    async def main() -> httpx2.Response:
        with ControlPlane(sdlc(), database=sqlite_url(tmp_path / "w.db"), migrate=True) as plane:
            async with http_client(plane) as client:
                body = {"supervisor": "box-1", "states": ["coding"], "wait": 31}
                return await client.post("/tasks/claim", json=body)

    answer = asyncio.run(main())

    assert answer.status_code == 422
    assert answer.json()["code"] == "invalid_argument"


def test_an_in_process_claim_waits_for_work(tmp_path: Path) -> None:
    async def main() -> tuple[TaskGrant | None, float]:
        with ControlPlane(sdlc(), database=sqlite_url(tmp_path / "w.db"), migrate=True) as plane:
            transport = InProcess(plane)
            started = time.monotonic()
            claiming = asyncio.create_task(transport.claim("box-1", ["coding"], wait=5))
            await asyncio.sleep(0.2)
            await asyncio.to_thread(plane.start, "T-1", TICKET)
            return await claiming, time.monotonic() - started

    grant, took = asyncio.run(main())

    assert grant is not None
    assert took < 2


def test_a_stop_with_the_default_idle_ends_within_two_seconds(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    folder = folder_for(tmp_path, graph)

    async def main() -> float:
        with ControlPlane(graph, database=database(tmp_path), migrate=True) as plane:
            stop = asyncio.Event()
            running = asyncio.create_task(supervise(folder, InProcess(plane), stop=stop))
            await asyncio.sleep(0.5)  # the supervisor now waits in a claim
            stopped = time.monotonic()
            stop.set()
            await asyncio.wait_for(running, timeout=10)
            return time.monotonic() - stopped

    assert asyncio.run(main()) < 2


def test_serve_stops_at_once_while_a_claim_waits(tmp_path: Path) -> None:
    graph = tmp_path / "sdlc.json"
    graph.write_text(json.dumps(sdlc()), encoding="utf-8")
    serving = prepare_serve(graph, sqlite_url(tmp_path / "s.db"), "127.0.0.1:0")
    server = GracefulServer(serving.config)
    server.on_exit = serving.waiting.close
    thread = threading.Thread(target=server.run, kwargs={"sockets": [serving.listener]})
    thread.start()
    try:
        wait_for(server_ready, server, thread)
        body = {"supervisor": "box-1", "states": ["coding"], "wait": 25}
        claim = threading.Thread(
            target=lambda: httpx2.post(f"{serving.url}/tasks/claim", json=body, timeout=40)
        )
        claim.start()
        time.sleep(0.5)  # the claim waits in the server now
        stopping = time.monotonic()
        server.should_exit = True
        thread.join(timeout=20)
        took = time.monotonic() - stopping
        claim.join(timeout=20)
    finally:
        serving.close()

    assert took < 3


def test_a_stop_waits_at_most_idle_for_the_claim_in_flight(tmp_path: Path) -> None:
    """A stop does not cut a claim in flight (the control plane can grant a task in it),
    so with `idle=3` the stop takes about 3 seconds, and never much more."""
    graph = compile_flow(tmp_path, FLOW)
    folder = folder_for(tmp_path, graph)

    async def main() -> float:
        with ControlPlane(graph, database=database(tmp_path), migrate=True) as plane:
            stop = asyncio.Event()
            running = asyncio.create_task(supervise(folder, InProcess(plane), stop=stop, idle=3))
            await asyncio.sleep(0.5)  # the supervisor now waits in a claim
            stopped = time.monotonic()
            stop.set()
            await asyncio.wait_for(running, timeout=20)
            return time.monotonic() - stopped

    assert asyncio.run(main()) < 3 + 1.5
