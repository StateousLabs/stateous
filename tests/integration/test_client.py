"""The client: a program outside calls the control plane over HTTP
(see the docs, Reference > The HTTP API)."""

import socket
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest

from stateous import Client
from stateous.client import ClientError, NotFound, Refused, Unreachable
from stateous.control.engine import Engine
from stateous.model.wire import Outcome


@pytest.fixture
def client(served: str) -> Iterator[Client]:
    with Client(served) as opened:
        yield opened


def nowhere() -> str:
    """A URL that nothing listens on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}"


def answering(status: int, text: str) -> Client:
    """A client whose control plane answers every request with `status` and `text`."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, text=text)

    client = Client("http://elsewhere.test")
    client._http = httpx2.Client(  # pyright: ignore[reportPrivateUsage]
        base_url="http://elsewhere.test", transport=httpx2.MockTransport(answer)
    )
    return client


def test_start_get_list_and_history(client: Client) -> None:
    token = client.start("T-1", {"repo": "acme/api"})

    assert (token["state"], token["status"]) == ("coding", "active")
    assert client.get("T-1")["token"] == "T-1"
    assert [t["token"] for t in client.tokens()] == ["T-1"]
    assert client.tokens(status="completed") == []
    assert client.history("T-1")[0]["kind"] == "started"


def test_a_listing_returns_at_most_its_limit(client: Client) -> None:
    for n in range(3):
        client.start(f"T-{n}", {"repo": "acme/api"})

    assert len(client.tokens(limit=2)) == 2
    assert len(client.tokens()) == 3


def test_starting_a_token_twice_returns_the_same_token(client: Client) -> None:
    first = client.start("T-1", {"repo": "acme/api"})

    assert client.start("T-1", {"repo": "acme/api"}) == first


def test_fire_with_a_key_is_applied_once(client: Client) -> None:
    client.start("T-1", {"repo": "acme/api"})

    first = client.fire("T-1", "done", key="gh-delivery-81231")
    second = client.fire("T-1", "done", key="gh-delivery-81231")

    assert first["state"] == "testing"
    assert second["duplicate"] is True
    assert second["token"]["state"] == "testing"


def test_fire_with_a_payload_ends_the_token(client: Client) -> None:
    client.start("T-1", {"repo": "acme/api"})
    client.fire("T-1", "done")

    assert client.fire("T-1", "passed", {"passed": 3, "failed": 0})["status"] == "completed"


def test_pause_resume_and_cancel(client: Client) -> None:
    client.start("T-1", {"repo": "acme/api"})

    assert client.pause("T-1")["status"] == "paused"
    assert client.resume("T-1")["status"] == "active"
    assert client.cancel("T-1")["status"] == "cancelled"


def test_report_moves_a_task_that_waits_for_the_outside(client: Client, engine: Engine) -> None:
    client.start("T-1", {"repo": "acme/api"})
    grant = engine.claim("box-1", ["coding"])
    assert grant is not None
    engine.report(grant.task, Outcome(kind="waiting"), supervisor="box-1")

    assert client.report(grant.task, "done")["state"] == "testing"


def test_report_with_an_attempt_is_refused_for_an_attempt_that_is_over(
    client: Client, engine: Engine
) -> None:
    client.start("T-1", {"repo": "acme/api"})
    first = engine.claim("box-1", ["coding"])
    assert first is not None
    engine.report(first.task, Outcome(kind="failed", error="x"), supervisor="box-1")
    again = engine.claim("box-1", ["coding"])
    assert again is not None
    engine.report(again.task, Outcome(kind="waiting"), supervisor="box-1")

    with pytest.raises(Refused) as refused:
        client.report(again.task, "done", attempt=1)

    assert (refused.value.status, refused.value.code) == (409, "attempt_over")
    assert client.get("T-1")["state"] == "coding"
    assert client.report(again.task, "done", attempt=2)["state"] == "testing"


def test_workflow_returns_the_graph(client: Client, sdlc: dict[str, Any]) -> None:
    assert client.workflow() == sdlc


def test_an_unknown_token_is_not_found(client: Client) -> None:
    with pytest.raises(NotFound) as caught:
        client.get("NOPE")

    assert caught.value.status == 404
    assert caught.value.code == "token_not_found"
    assert caught.value.message
    assert "(404)" in str(caught.value)


def test_an_id_with_a_slash_stays_one_segment_and_is_not_found(client: Client) -> None:
    with pytest.raises(NotFound):
        client.get("a/b")


def test_a_refusal_carries_the_status_and_the_reason(client: Client) -> None:
    client.start("T-1", {"repo": "acme/api"})

    with pytest.raises(Refused) as caught:
        client.fire("T-1", "passed", {"passed": 1, "failed": 0})

    assert caught.value.status == 409
    assert caught.value.code == "event_not_allowed"
    assert not isinstance(caught.value, NotFound)
    assert caught.value.message in str(caught.value)


def test_a_control_plane_that_does_not_answer_is_unreachable() -> None:
    with Client(nowhere()) as client, pytest.raises(Unreachable) as caught:
        client.tokens()

    assert "cannot reach the control plane" in str(caught.value)


def test_an_answer_that_is_not_json_is_a_client_error() -> None:
    with answering(200, "<html>a proxy</html>") as client, pytest.raises(ClientError) as caught:
        client.workflow()

    assert "not JSON" in str(caught.value)


def test_a_refusal_that_is_not_json_keeps_its_text() -> None:
    with answering(502, "bad gateway") as client, pytest.raises(Refused) as caught:
        client.get("T-1")

    assert (caught.value.status, caught.value.message) == (502, "bad gateway")
    assert caught.value.code is None


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ('{"error": "no", "code": "task_not_held"}', "task_not_held"),
        ('{"error": "no"}', None),
        ('{"error": "no", "code": 7}', None),
    ],
)
def test_a_refusal_carries_the_code_of_its_body(body: str, code: str | None) -> None:
    with answering(409, body) as client, pytest.raises(Refused) as caught:
        client.get("T-1")

    assert (caught.value.status, caught.value.message, caught.value.code) == (409, "no", code)


def test_a_url_that_is_not_a_url_is_one_client_error() -> None:
    with pytest.raises(ClientError) as caught:
        Client("http://[::1")

    assert str(caught.value).startswith("http://[::1 is not a URL: ")
    assert "\n" not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://my host:8000",  # a space
        "http://local%host:8000",  # a '%'
    ],
)
def test_a_url_with_a_host_that_is_not_valid_is_one_client_error(url: str) -> None:
    with pytest.raises(ClientError) as caught:
        Client(url)

    assert str(caught.value) == (
        f"{url} has a host that is not valid: a host has only letters, digits, '-' and "
        "'.', or is an IP address"
    )
    assert "\n" not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
        "http://my_host:8000",  # '_' is not RFC 1123, but Docker uses it: allowed
    ],
)
def test_a_url_with_a_valid_host_is_taken(url: str) -> None:
    Client(url).close()


def test_a_request_url_that_cannot_be_built_is_one_client_error() -> None:
    with Client(nowhere()) as client, pytest.raises(ClientError) as caught:
        client.get("a" * 70_000)  # longer than a URL can be

    assert " is not a URL: " in str(caught.value)
    assert not isinstance(caught.value, Unreachable)
