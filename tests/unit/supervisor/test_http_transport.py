"""`HttpTransport`: one attempt for each call, with headers, a path and a gateway's
answers."""

import asyncio
from pathlib import Path
from typing import Any

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount

from stateous import ControlPlane
from stateous.control.api import create_app
from stateous.model.wire import Outcome
from stateous.supervisor.remote import HttpTransport
from stateous.transport import NotReached, Refused

from ..control.support import TICKET, sdlc, sqlite_url


def http(handler: Any, url: str = "http://cp.test", **options: Any) -> HttpTransport:
    client = httpx2.AsyncClient(base_url=url, transport=httpx2.MockTransport(handler))
    return HttpTransport(url, client=client, **options)


def test_a_codeless_error_is_refused_with_its_status_in_the_message() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, text="<html>log in</html>")

    with pytest.raises(Refused) as refused:
        asyncio.run(http(handler).note(1, "x", attempt=1))

    assert refused.value.code == "unexpected"
    assert refused.value.message.endswith("(HTTP 401)")


@pytest.mark.parametrize(
    ("status", "answer"),
    [
        (503, {"error": "another control plane took this database", "code": "not_serving"}),
        (503, {"error": "busy", "code": "database_busy"}),
        (502, None),
    ],
)
def test_a_retry_answer_is_not_reached(status: int, answer: Any) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json=answer)

    with pytest.raises(NotReached):
        asyncio.run(http(handler).note(1, "x", attempt=1))


def test_a_transport_error_is_not_reached() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    with pytest.raises(NotReached, match=r"cannot reach the control plane at http://cp\.test"):
        asyncio.run(http(handler).health())


def test_http_sends_the_headers_of_each_call() -> None:
    seen: list[str | None] = []
    tokens = iter(["t-1", "t-2"])

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("authorization"))
        return httpx2.Response(204)

    transport = http(handler, headers=lambda: {"Authorization": f"Bearer {next(tokens)}"})

    async def run() -> None:
        await transport.note(1, "x", attempt=1)
        await transport.note(1, "y", attempt=1)

    asyncio.run(run())

    assert seen == ["Bearer t-1", "Bearer t-2"]


def test_http_keeps_the_path_of_the_url() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(204)

    asyncio.run(http(handler, url="https://gw.example/x/plan").claim("box-1", ["coding"]))

    assert seen == ["https://gw.example/x/plan/tasks/claim"]


def test_a_claim_sends_wait_only_when_it_waits() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(request.content)
        return httpx2.Response(204)

    async def run() -> None:
        transport = http(handler)
        await transport.claim("box-1", ["coding"])
        await transport.claim("box-1", ["coding"], wait=5)

    asyncio.run(run())

    assert b'"wait"' not in bodies[0]
    assert b'"wait": 5' in bodies[1] or b'"wait":5' in bodies[1]


def test_the_app_works_under_a_prefix(tmp_path: Path) -> None:
    with ControlPlane(sdlc(), database=sqlite_url(tmp_path / "s.db"), migrate=True) as plane:
        plane.start("T-1", TICKET)
        app = Starlette(routes=[Mount("/plan", app=create_app(plane))])
        client = httpx2.AsyncClient(
            base_url="http://testserver/plan", transport=httpx2.ASGITransport(app=app)
        )
        transport = HttpTransport("http://testserver/plan", client=client)

        async def run() -> None:
            grant = await transport.claim("box-1", ["coding"])
            assert grant is not None
            assert await transport.heartbeat("box-1", [grant.task]) == []
            outcome = Outcome(kind="event", event="stuck", attempt=grant.attempt)
            await transport.report(grant.task, outcome, supervisor="box-1")
            await transport.aclose()

        asyncio.run(run())

        assert plane.get("T-1").status == "completed"


@pytest.mark.parametrize("status", [408, 429])
def test_a_gateway_that_says_later_is_not_reached(status: int) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, text="slow down")

    with pytest.raises(NotReached):
        asyncio.run(http(handler).note(1, "x", attempt=1))


def test_headers_that_cannot_be_made_are_not_reached() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(204)

    def expired() -> dict[str, str]:
        raise OSError("the token service is down")

    with pytest.raises(NotReached, match="the token service is down"):
        asyncio.run(http(handler, headers=expired).note(1, "x", attempt=1))
