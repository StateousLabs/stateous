"""Fixtures for the API, the client and the commands: one workflow document, an
engine over it, the API in process (`http`), and the API on a real port (`served`)."""

import copy
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from starlette.testclient import TestClient

from stateous.control.api import create_app
from stateous.control.engine import Engine
from stateous.control.graph import Graph
from stateous.control.store import Store

SDLC: dict[str, Any] = {
    "stateous": 1,
    "workflow": "sdlc",
    "context": "Ticket",
    "states": {
        "coding": {"timeout": "6h", "retries": 1, "emits": ["done", "stuck"]},
        "testing": {"emits": ["passed", "failed"]},
    },
    "events": {
        "done": {},
        "stuck": {},
        "passed": {"payload": "Tests"},
        "failed": {"payload": "Tests"},
    },
    "transitions": [
        {"from": "__start__", "to": "coding"},
        {"from": "coding", "to": "testing", "on": "done"},
        {"from": "coding", "to": "__end__", "on": "stuck"},
        {"from": "testing", "to": "__end__", "on": "passed"},
        {"from": "testing", "to": "coding", "on": "failed"},
    ],
    "schemas": {
        "Ticket": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
        },
        "Tests": {
            "type": "object",
            "properties": {"passed": {"type": "integer"}, "failed": {"type": "integer"}},
            "required": ["passed", "failed"],
        },
    },
}


@pytest.fixture
def sdlc() -> dict[str, Any]:
    """The sdlc example's graph document, without guards. A fresh copy: a test may
    change it."""
    return copy.deepcopy(SDLC)


@pytest.fixture
def engine(sdlc: dict[str, Any]) -> Iterator[Engine]:
    store = Store("sqlite://")
    yield Engine(store, Graph(sdlc))
    store.close()


@pytest.fixture
def http(engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(engine)) as client:
        yield client


@pytest.fixture
def served(engine: Engine) -> Iterator[str]:
    """The API on a real port, in a thread: its URL."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(create_app(engine), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive():
                pytest.fail("the server thread died before the server started")
            if time.monotonic() > deadline:
                pytest.fail("the server did not start within 10 seconds")
            time.sleep(0.01)
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
