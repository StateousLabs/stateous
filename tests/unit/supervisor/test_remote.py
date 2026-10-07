"""The control plane's HTTP API, as the supervisor calls it."""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from stateous.model.wire import Outcome, Schedule, TaskGrant
from stateous.supervisor.remote import ControlPlaneApi, Refused

GRANT = TaskGrant(
    task=7,
    token="T-1",
    state="coding",
    visit=1,
    attempt=1,
    context={"repo": "acme/api"},
    memory={"dev": "s-1"},
    entered_by=None,
)

Handler = Callable[[httpx2.Request], httpx2.Response]


def api_with(handler: Handler, delays: list[float] | None = None) -> ControlPlaneApi:
    async def sleep(seconds: float) -> None:
        if delays is not None:
            delays.append(seconds)

    client = httpx2.AsyncClient(base_url="http://cp.test", transport=httpx2.MockTransport(handler))
    return ControlPlaneApi("http://cp.test/", client=client, sleep=sleep)


def body(request: httpx2.Request) -> Any:
    return json.loads(request.content) if request.content else None


def test_a_claim_with_no_task_is_none_and_a_claim_with_a_task_is_its_grant() -> None:
    seen: list[tuple[str, str, Any]] = []
    answers = [httpx2.Response(204), httpx2.Response(200, json=GRANT.to_json())]

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.method, request.url.path, body(request)))
        return answers.pop(0)

    async def scenario() -> tuple[TaskGrant | None, TaskGrant | None]:
        api = api_with(handler)
        try:
            return await api.claim("box-1", ["coding"]), await api.claim("box-1", ["coding"])
        finally:
            await api.aclose()

    first, second = asyncio.run(scenario())

    assert (first, second) == (None, GRANT)
    assert seen[0] == ("POST", "/tasks/claim", {"supervisor": "box-1", "states": ["coding"]})


def test_report_heartbeat_note_and_memory_use_the_contracts_paths_and_bodies() -> None:
    seen: list[tuple[str, str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.method, request.url.path, body(request)))
        if request.url.path == "/tasks/heartbeat":
            return httpx2.Response(200, json={"lost": [3]})
        if request.url.path.endswith("/report"):
            return httpx2.Response(200, json={"token": "T-1"})
        return httpx2.Response(204)

    outcome = Outcome(
        kind="waiting",
        attempt=2,
        context={"note": "x"},
        schedules=(Schedule(event="remind", payload=None, after="10m"),),
    )

    async def scenario() -> list[int]:
        api = api_with(handler)
        try:
            await api.report(7, outcome, supervisor="box-1")
            lost = await api.heartbeat("box-1", [7, 3])
            await api.note(7, "half done", attempt=2)
            await api.set_memory(7, "dev", "s-1", attempt=2)
            await api.set_memory(7, "dev", None, attempt=2)
            return lost
        finally:
            await api.aclose()

    assert asyncio.run(scenario()) == [3]
    assert seen == [
        ("POST", "/tasks/7/report", {**outcome.to_json(), "supervisor": "box-1"}),
        ("POST", "/tasks/heartbeat", {"supervisor": "box-1", "tasks": [7, 3]}),
        ("POST", "/tasks/7/note", {"text": "half done", "attempt": 2}),
        ("PUT", "/tasks/7/memory", {"key": "dev", "value": "s-1", "attempt": 2}),
        ("PUT", "/tasks/7/memory", {"key": "dev", "value": None, "attempt": 2}),
    ]


def test_health_is_the_control_planes_answer() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"workflow": "sdlc", "checksum": "sha256:x"})

    async def scenario() -> dict[str, Any]:
        api = api_with(handler)
        try:
            return await api.health()
        finally:
            await api.aclose()

    assert asyncio.run(scenario()) == {"workflow": "sdlc", "checksum": "sha256:x"}


def test_an_unreachable_control_plane_is_tried_again_with_one_log_line_each_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="stateous.supervisor")
    delays: list[float] = []
    failures = [httpx2.ConnectError("connection refused"), None, None]
    statuses = [0, 503, 204]

    def handler(request: httpx2.Request) -> httpx2.Response:
        failure, status = failures.pop(0), statuses.pop(0)
        if failure is not None:
            raise failure
        return httpx2.Response(status)

    async def scenario() -> None:
        api = api_with(handler, delays)
        try:
            await api.note(7, "still here", attempt=1)
        finally:
            await api.aclose()

    asyncio.run(scenario())

    assert delays == [1.0, 2.0]
    lines = [r.getMessage() for r in caplog.records if "cannot reach" in r.getMessage()]
    assert len(lines) == 2
    assert "http://cp.test" in lines[0] and "connection refused" in lines[0]
    assert "it answered 503" in lines[1]


def test_the_backoff_grows_to_30_seconds_and_stays_there() -> None:
    delays: list[float] = []
    calls = [0]

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls[0] += 1
        if calls[0] <= 8:
            raise httpx2.ConnectError("down")
        return httpx2.Response(204)

    async def scenario() -> None:
        api = api_with(handler, delays)
        try:
            await api.note(1, "x", attempt=1)
        finally:
            await api.aclose()

    asyncio.run(scenario())

    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


@pytest.mark.parametrize(
    ("status", "answer", "message"),
    [
        (409, {"error": "task 7 is not this supervisor's"}, "task 7 is not this supervisor's"),
        (422, {"error": "text is empty"}, "text is empty"),
        (404, None, "Not Found"),
        (500, None, "the control plane answered 500"),
    ],
)
def test_a_refusal_is_not_tried_again(status: int, answer: Any, message: str) -> None:
    delays: list[float] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if answer is not None:
            return httpx2.Response(status, json=answer)
        return httpx2.Response(status, text="Not Found" if status == 404 else "")

    async def scenario() -> None:
        api = api_with(handler, delays)
        try:
            await api.note(7, "x", attempt=1)
        finally:
            await api.aclose()

    with pytest.raises(Refused) as refused:
        asyncio.run(scenario())

    assert (refused.value.status, refused.value.message, delays) == (status, message, [])


def test_a_claim_that_waits_to_try_again_gives_up_when_stop_is_set() -> None:
    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.url.path)
        raise httpx2.ConnectError("down")

    async def never(seconds: float) -> None:
        await asyncio.Event().wait()  # a backoff that does not end by itself

    async def scenario() -> TaskGrant | None:
        client = httpx2.AsyncClient(
            base_url="http://cp.test", transport=httpx2.MockTransport(handler)
        )
        api = ControlPlaneApi("http://cp.test", client=client, sleep=never)
        stop = asyncio.Event()
        try:
            claiming = asyncio.create_task(api.claim("box-1", ["coding"], stop=stop))
            while not requests:
                await asyncio.sleep(0)
            stop.set()
            return await asyncio.wait_for(claiming, 5.0)
        finally:
            await api.aclose()

    assert asyncio.run(scenario()) is None
    assert requests == ["/tasks/claim"]


def test_a_claim_does_not_try_again_after_stop_is_set() -> None:
    requests: list[str] = []
    delays: list[float] = []
    stop = asyncio.Event()

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.url.path)
        stop.set()  # the stop comes while the request is in flight
        return httpx2.Response(503)

    async def scenario() -> TaskGrant | None:
        api = api_with(handler, delays)
        try:
            return await api.claim("box-1", ["coding"], stop=stop)
        finally:
            await api.aclose()

    assert asyncio.run(scenario()) is None
    assert (requests, delays) == (["/tasks/claim"], [])
