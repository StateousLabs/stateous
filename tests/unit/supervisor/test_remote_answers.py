"""The control plane's answers that the supervisor cannot use, and a retry with a stop event."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx2
import pytest

from stateous.supervisor.remote import Refused

from .fakes import GRANT, Handler, api_with


def run_claim(handler: Handler) -> None:
    async def scenario() -> None:
        api = api_with(handler)
        try:
            await api.claim("box-1", ["coding"])
        finally:
            await api.aclose()

    asyncio.run(scenario())


def test_a_claimed_task_that_is_not_valid_is_refused_with_the_reason() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"task": "seven"})

    with pytest.raises(Refused) as refused:
        run_claim(handler)

    assert refused.value.status == 200
    assert refused.value.message.startswith("the claimed task is not valid: ")


def test_a_claim_answer_that_is_not_json_is_refused() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<html>hello</html>")

    with pytest.raises(Refused) as refused:
        run_claim(handler)

    assert refused.value.message == "the control plane's answer is not JSON"


def test_a_health_answer_that_is_not_an_object_is_refused() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=[GRANT.task])

    async def scenario() -> dict[str, Any]:
        api = api_with(handler)
        try:
            return await api.health()
        finally:
            await api.aclose()

    with pytest.raises(Refused) as refused:
        asyncio.run(scenario())

    assert refused.value.message == "the control plane's answer is not a JSON object"


@pytest.mark.parametrize("answer", [{"lost": "7"}, {"other": []}])
def test_a_heartbeat_answer_without_a_list_of_lost_tasks_is_refused(answer: dict[str, Any]) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=answer)

    async def scenario() -> list[int]:
        api = api_with(handler)
        try:
            return await api.heartbeat("box-1", [7])
        finally:
            await api.aclose()

    with pytest.raises(Refused) as refused:
        asyncio.run(scenario())

    assert refused.value.message == "the heartbeat answer has no list of lost tasks"


def test_an_unavailable_answer_with_a_code_is_logged_with_the_code_and_the_sentence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="stateous.supervisor")
    answers = [
        httpx2.Response(503, json={"error": "starting up", "code": "warming"}),
        httpx2.Response(204),
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        return answers.pop(0)

    async def scenario() -> None:
        api = api_with(handler, [])
        try:
            await api.note(7, "x", attempt=1)
        finally:
            await api.aclose()

    asyncio.run(scenario())

    lines = [r.getMessage() for r in caplog.records if "trying again" in r.getMessage()]
    assert lines == [
        "cannot reach the control plane at http://cp.test "
        "(it answered 503, warming: starting up); trying again in 1s"
    ]


def test_a_call_with_a_stop_event_that_is_not_set_waits_and_tries_again() -> None:
    answers = [httpx2.Response(503), httpx2.Response(204)]
    delays: list[float] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        return answers.pop(0)

    async def scenario() -> None:
        api = api_with(handler, delays)
        try:
            await api.note(7, "x", attempt=1, stop=asyncio.Event())
        finally:
            await api.aclose()

    asyncio.run(scenario())

    assert delays == [1.0]
    assert answers == []
