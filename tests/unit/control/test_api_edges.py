"""API edges: the refusals that a client reads, and the body guard on a lost client."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from stateous.control import api
from stateous.control.api import BodyLimit, create_app
from stateous.control.engine import Engine

from .support import TICKET, new_engine


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = new_engine()
    yield engine
    engine.store.close()


@pytest.fixture
def http(engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(engine)) as client:
        yield client


def test_an_idempotency_key_with_a_control_character_is_refused(http: TestClient) -> None:
    http.post("/tokens", json={"token": "T-1", "context": TICKET})

    answer = http.post("/tokens/T-1/events/done", headers={"Idempotency-Key": "a\tb"})

    assert answer.status_code == 422
    assert answer.json() == {
        "error": "an idempotency key has no control characters",
        "code": "invalid_payload",
    }


def test_a_body_nested_too_deep_is_refused_by_the_route(http: TestClient) -> None:
    raw = b"[" * 200_000 + b"]" * 200_000

    answer = http.post("/tokens", content=raw)

    assert answer.status_code == 400
    assert answer.json()["error"] == (
        f"the body is not JSON: it is nested more than {api.MAX_DEPTH} levels deep"
    )


def test_a_client_that_goes_away_while_the_body_arrives_reaches_the_route() -> None:
    seen: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(await receive())

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        raise AssertionError("the guard answered a client that went away")

    scope: Scope = {"type": "http", "headers": []}
    asyncio.run(BodyLimit(app)(scope, receive, send))

    assert seen == [{"type": "http.disconnect"}]
