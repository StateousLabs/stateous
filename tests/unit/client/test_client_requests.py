"""The client's requests and the answers it cannot use, with a fake control plane."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from stateous import Client
from stateous.client import ClientError

URL = "http://elsewhere.test"


def client_for(seen: list[httpx2.Request], answer: httpx2.Response) -> Client:
    """A client whose control plane records each request and sends `answer`."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return answer

    client = Client(URL)
    client.close()
    client._http = httpx2.Client(  # pyright: ignore[reportPrivateUsage]
        base_url=URL, transport=httpx2.MockTransport(handler)
    )
    return client


def test_fire_with_a_payload_sends_the_payload_in_the_body() -> None:
    seen: list[httpx2.Request] = []
    client = client_for(seen, httpx2.Response(200, json={"token": "T-1"}))

    assert client.fire("T-1", "done", {"score": 3}) == {"token": "T-1"}

    assert seen[0].url.path == "/tokens/T-1/events/done"
    assert json.loads(seen[0].content) == {"payload": {"score": 3}}


def test_tokens_with_a_parent_sends_the_parent_in_the_query() -> None:
    seen: list[httpx2.Request] = []
    client = client_for(seen, httpx2.Response(200, json=[]))

    assert client.tokens(parent="T-0") == []

    assert dict(seen[0].url.params) == {"parent": "T-0"}


@pytest.mark.parametrize(
    ("payload", "attempt", "body"),
    [
        (None, None, {"kind": "event", "event": "done"}),
        ({"score": 3}, None, {"kind": "event", "event": "done", "payload": {"score": 3}}),
        (None, 2, {"kind": "event", "event": "done", "attempt": 2}),
    ],
)
def test_a_report_sends_the_payload_and_the_attempt_only_when_they_are_given(
    payload: dict[str, Any] | None, attempt: int | None, body: dict[str, Any]
) -> None:
    seen: list[httpx2.Request] = []
    client = client_for(seen, httpx2.Response(200, json={"token": "T-1"}))

    client.report(7, "done", payload, attempt=attempt)

    assert seen[0].url.path == "/tasks/7/report"
    assert json.loads(seen[0].content) == body


def test_an_answer_that_is_not_an_object_is_refused_with_one_sentence() -> None:
    client = client_for([], httpx2.Response(200, json=["not", "an", "object"]))

    with pytest.raises(ClientError) as error:
        client.get("T-1")

    assert str(error.value) == (
        f"the control plane at {URL} sent an answer of the wrong shape; check the URL"
    )


def test_an_answer_that_is_not_a_list_is_refused_with_one_sentence() -> None:
    client = client_for([], httpx2.Response(200, json={"not": "a list"}))

    with pytest.raises(ClientError) as error:
        client.tokens()

    assert str(error.value) == (
        f"the control plane at {URL} sent an answer of the wrong shape; check the URL"
    )
