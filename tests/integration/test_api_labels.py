"""Labels over HTTP, through the client, and the client's headers."""

from __future__ import annotations

from typing import Any

import httpx2
from starlette.testclient import TestClient

from stateous import Client

U1 = {"user": "u1", "project": "p7"}


def test_post_tokens_takes_labels_and_get_filters_by_them(http: TestClient) -> None:
    first = http.post("/tokens", json={"token": "T-1", "context": {"repo": "a"}, "labels": U1})
    http.post("/tokens", json={"token": "T-2", "context": {"repo": "b"}})

    listed = http.get("/tokens", params=[("label", "user=u1"), ("label", "project=p7")])

    assert first.status_code == 201
    assert first.json()["labels"] == U1
    assert [token["token"] for token in listed.json()] == ["T-1"]
    assert "labels" not in http.get("/tokens/T-2").json()


def test_bad_labels_are_422(http: TestClient) -> None:
    answer = http.post("/tokens", json={"token": "T-1", "context": {}, "labels": {"Bad": "v"}})
    listed = http.get("/tokens", params={"label": "no-equals-sign"})

    assert (answer.status_code, answer.json()["code"]) == (422, "invalid_argument")
    assert (listed.status_code, listed.json()["code"]) == (422, "invalid_argument")


def test_the_client_starts_and_lists_by_labels(served: str) -> None:
    with Client(served) as client:
        client.start("T-1", {"repo": "a"}, labels=U1)
        client.start("T-2", {"repo": "b"}, labels={"user": "u2"})

        found = client.tokens(labels={"user": "u1"})

    assert [token["token"] for token in found] == ["T-1"]
    assert found[0]["labels"] == U1


def test_the_client_sends_its_headers_on_each_call() -> None:
    seen: list[str | None] = []
    tokens = iter(["a", "b"])

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("x-user"))
        return httpx2.Response(200, json={"token": "T-1"})

    client = Client("http://elsewhere.test", headers=lambda: {"X-User": next(tokens)})
    transport: Any = httpx2.MockTransport(answer)
    client._http = httpx2.Client(  # pyright: ignore[reportPrivateUsage]
        base_url="http://elsewhere.test", transport=transport
    )
    client.get("T-1")
    client.get("T-1")

    assert seen == ["a", "b"]
