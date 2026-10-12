"""The HTTP API's token routes, in process (see the guide, "The HTTP API")."""

from collections.abc import Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

from stateous.control.api import MAX_BODY
from stateous.control.engine import Engine
from stateous.model.graph_file import checksum


def started(http: TestClient, token: str = "T-1") -> dict[str, Any]:
    answer = http.post("/tokens", json={"token": token, "context": {"repo": "acme/api"}})
    assert answer.status_code == 201, answer.text
    return answer.json()


def test_a_new_token_enters_the_first_state(http: TestClient) -> None:
    token = started(http)

    assert (token["token"], token["state"], token["status"], token["visit"]) == (
        "T-1", "coding", "active", 1,
    )  # fmt: skip
    assert token["context"] == {"repo": "acme/api"}
    assert token["entered_at"].endswith("Z")


def test_starting_a_token_that_exists_returns_it_with_200(http: TestClient) -> None:
    started(http)

    again = http.post("/tokens", json={"token": "T-1", "context": {"repo": "acme/api"}})

    assert again.status_code == 200
    assert again.json()["token"] == "T-1"


def test_a_context_that_does_not_fit_the_model_is_422(http: TestClient) -> None:
    answer = http.post("/tokens", json={"token": "T-1", "context": {"repo": 7}})

    assert answer.status_code == 422
    assert isinstance(answer.json()["error"], str)


@pytest.mark.parametrize("token", ["", "a/b", "has space", "-dash-first", "x" * 129])
def test_a_token_id_that_the_engine_refuses_is_422(http: TestClient, token: str) -> None:
    answer = http.post("/tokens", json={"token": token, "context": {"repo": "acme/api"}})

    assert answer.status_code == 422
    assert isinstance(answer.json()["error"], str)


def test_a_token_id_that_differs_only_in_case_is_422(http: TestClient) -> None:
    started(http, "T-1")

    answer = http.post("/tokens", json={"token": "t-1", "context": {"repo": "acme/api"}})

    assert answer.status_code == 422
    assert isinstance(answer.json()["error"], str)


@pytest.mark.parametrize("token", [7, None])
def test_a_token_id_that_is_not_a_string_is_422(http: TestClient, token: object) -> None:
    answer = http.post("/tokens", json={"token": token})

    assert answer.status_code == 422
    assert "'token'" in answer.json()["error"]


def test_a_context_that_is_not_an_object_is_422(http: TestClient) -> None:
    answer = http.post("/tokens", json={"token": "T-1", "context": ["acme/api"]})

    assert answer.status_code == 422
    assert "'context'" in answer.json()["error"]


def test_tokens_are_listed_and_filtered_by_status(http: TestClient) -> None:
    started(http, "T-1")
    started(http, "T-2")
    http.post("/tokens/T-2/cancel")

    assert [t["token"] for t in http.get("/tokens?status=active").json()] == ["T-1"]
    assert {t["token"] for t in http.get("/tokens").json()} == {"T-1", "T-2"}
    assert len(http.get("/tokens?limit=1").json()) == 1


@pytest.mark.parametrize(
    "query", ["status=done", "limit=0", "limit=abc", "limit=1001", "limit=" + "9" * 30]
)
def test_a_wrong_status_or_limit_is_422(http: TestClient, query: str) -> None:
    answer = http.get(f"/tokens?{query}")

    assert answer.status_code == 422
    assert isinstance(answer.json()["error"], str)


def test_an_unknown_token_is_404_with_a_sentence(http: TestClient) -> None:
    for path in ("/tokens/NOPE", "/tokens/NOPE/history"):
        answer = http.get(path)
        assert answer.status_code == 404
        assert isinstance(answer.json()["error"], str)


def test_history_lists_the_ledger_entries_in_order(http: TestClient) -> None:
    started(http)

    entries = http.get("/tokens/T-1/history").json()

    assert entries[0]["kind"] == "started"
    assert "entered" in [e["kind"] for e in entries]
    assert [e["seq"] for e in entries] == sorted(e["seq"] for e in entries)
    assert all(e["at"].endswith("Z") for e in entries)
    assert all(set(e) == {"seq", "at", "kind", "data"} for e in entries)


def test_firing_an_event_moves_the_token(http: TestClient) -> None:
    started(http)

    answer = http.post("/tokens/T-1/events/done")

    assert answer.status_code == 200
    assert answer.json()["state"] == "testing"


def test_the_same_idempotency_key_fires_once(http: TestClient) -> None:
    started(http)

    first = http.post("/tokens/T-1/events/done", headers={"Idempotency-Key": "gh-1"})
    second = http.post("/tokens/T-1/events/done", headers={"Idempotency-Key": "gh-1"})

    assert first.json()["state"] == "testing"
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["token"]["state"] == "testing"


def test_an_idempotency_key_longer_than_256_characters_is_422(http: TestClient) -> None:
    started(http)

    answer = http.post("/tokens/T-1/events/done", headers={"Idempotency-Key": "k" * 257})

    assert answer.status_code == 422
    assert "256" in answer.json()["error"]


def test_an_event_the_state_does_not_allow_is_409(http: TestClient) -> None:
    started(http)

    answer = http.post("/tokens/T-1/events/passed", json={"payload": {"passed": 1, "failed": 0}})

    assert answer.status_code == 409


def test_a_payload_that_does_not_fit_is_422(http: TestClient) -> None:
    started(http)
    http.post("/tokens/T-1/events/done")

    answer = http.post("/tokens/T-1/events/passed", json={"payload": {"passed": "many"}})

    assert answer.status_code == 422


def test_a_payload_that_is_not_an_object_is_422(http: TestClient) -> None:
    started(http)

    assert http.post("/tokens/T-1/events/done", json={"payload": [1]}).status_code == 422


def test_pause_resume_and_cancel_steer_a_token(http: TestClient) -> None:
    started(http)

    assert http.post("/tokens/T-1/pause").json()["status"] == "paused"
    assert http.post("/tokens/T-1/resume").json()["status"] == "active"
    assert http.post("/tokens/T-1/cancel").json()["status"] == "cancelled"
    assert http.post("/tokens/T-1/events/done").status_code == 409
    assert http.post("/tokens/NOPE/pause").status_code == 404


def test_the_workflow_and_the_health_come_from_the_graph(
    http: TestClient, engine: Engine, sdlc: dict[str, Any]
) -> None:
    served = http.get("/workflow").json()
    health = http.get("/health").json()

    assert served == sdlc
    assert health == {"workflow": "sdlc", "checksum": engine.graph.checksum}
    assert health["checksum"] == checksum(served)  # contract section 18, R6


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (b"{not json", 400),
        (b'{"token": NaN}', 400),
        (b"[" * 70 + b"]" * 70, 400),
        (b"[1, 2]", 422),
    ],
)
def test_a_body_that_is_not_a_json_object_is_refused_with_a_sentence(
    http: TestClient, body: bytes, status: int
) -> None:
    answer = http.post("/tokens", content=body, headers={"content-type": "application/json"})

    assert answer.status_code == status
    assert isinstance(answer.json()["error"], str)


def test_a_body_larger_than_1_mib_is_413(http: TestClient) -> None:
    answer = http.post(
        "/tokens", content=b" " * (MAX_BODY + 1), headers={"content-type": "application/json"}
    )

    assert answer.status_code == 413
    assert "1 MiB" in answer.json()["error"]


def test_a_chunked_body_larger_than_1_mib_is_413(http: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(3):
            yield b" " * (512 * 1024)

    answer = http.post("/tokens", content=chunks(), headers={"content-type": "application/json"})

    assert answer.status_code == 413


def test_an_unknown_path_or_a_wrong_method_answers_json(http: TestClient) -> None:
    missing = http.get("/nothing")
    wrong = http.get("/tokens/T-1/pause")

    assert (missing.status_code, wrong.status_code) == (404, 405)
    assert "/nothing" in missing.json()["error"]
    assert "GET" in wrong.json()["error"]
