"""The HTTP API's task routes: what a supervisor calls, and what an outside actor
calls to report a task that waits for it."""

import json
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError
from starlette.testclient import TestClient

from stateous.control.engine import Engine


def claimed(http: TestClient, supervisor: str = "box-1") -> dict[str, Any]:
    started = http.post("/tokens", json={"token": "T-1", "context": {"repo": "acme/api"}})
    assert started.status_code == 201, started.text
    answer = http.post("/tasks/claim", json={"supervisor": supervisor, "states": ["coding"]})
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_a_claim_grants_the_task_of_a_served_state(http: TestClient) -> None:
    grant = claimed(http)

    assert (grant["token"], grant["state"], grant["visit"], grant["attempt"]) == (
        "T-1", "coding", 1, 1,
    )  # fmt: skip
    assert grant["context"] == {"repo": "acme/api"}
    assert grant["memory"] == {}
    assert isinstance(grant["task"], int)


def test_a_claim_with_nothing_to_do_is_204(http: TestClient) -> None:
    claimed(http)

    again = http.post("/tasks/claim", json={"supervisor": "box-1", "states": ["coding"]})
    other = http.post("/tasks/claim", json={"supervisor": "box-1", "states": ["testing"]})

    assert (again.status_code, other.status_code) == (204, 204)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"supervisor": "", "states": []},
        {"supervisor": "box-1", "states": "coding"},
        {"supervisor": "box-1", "states": [1]},
        {"supervisor": "b" * 257, "states": ["coding"]},
    ],
)
def test_a_claim_without_a_supervisor_and_states_is_422(
    http: TestClient, body: dict[str, Any]
) -> None:
    answer = http.post("/tasks/claim", json=body)

    assert answer.status_code == 422
    assert isinstance(answer.json()["error"], str)


def test_a_claim_accepts_a_supervisor_name_up_to_256_characters(http: TestClient) -> None:
    grant = claimed(http, supervisor="b" * 256)

    assert grant["token"] == "T-1"


def test_a_report_moves_the_token(http: TestClient) -> None:
    grant = claimed(http)

    answer = http.post(
        f"/tasks/{grant['task']}/report",
        json={"kind": "event", "event": "done", "supervisor": "box-1"},
    )

    assert answer.status_code == 200
    assert answer.json()["state"] == "testing"


def test_a_report_of_an_event_the_state_may_not_emit_is_409(http: TestClient) -> None:
    grant = claimed(http)

    answer = http.post(
        f"/tasks/{grant['task']}/report",
        json={
            "kind": "event",
            "event": "passed",
            "payload": {"passed": 1, "failed": 0},
            "supervisor": "box-1",
        },
    )

    assert answer.status_code == 409


def test_a_report_from_another_supervisor_is_409(http: TestClient) -> None:
    grant = claimed(http)

    answer = http.post(
        f"/tasks/{grant['task']}/report",
        json={"kind": "event", "event": "done", "supervisor": "box-2"},
    )

    assert answer.status_code == 409


@pytest.mark.parametrize("task", [999, 2**70])
def test_a_report_on_an_unknown_task_is_404(http: TestClient, task: int) -> None:
    claimed(http)

    answer = http.post(f"/tasks/{task}/report", json={"kind": "event", "event": "done"})

    assert answer.status_code == 404
    assert isinstance(answer.json()["error"], str)


@pytest.mark.parametrize(
    "body",
    [{"kind": "nope"}, {"event": "done"}, {"kind": "event", "event": "done", "supervisor": 3}],
)
def test_a_report_that_is_not_an_outcome_is_422(http: TestClient, body: dict[str, Any]) -> None:
    grant = claimed(http)

    answer = http.post(f"/tasks/{grant['task']}/report", json=body)

    assert answer.status_code == 422
    assert isinstance(answer.json()["error"], str)


def test_a_waiting_task_is_reported_later_from_outside(http: TestClient) -> None:
    grant = claimed(http)

    waiting = http.post(
        f"/tasks/{grant['task']}/report", json={"kind": "waiting", "supervisor": "box-1"}
    )
    later = http.post(f"/tasks/{grant['task']}/report", json={"kind": "event", "event": "done"})

    assert waiting.json()["state"] == "coding"
    assert later.json()["state"] == "testing"


def test_a_heartbeat_names_the_tasks_this_supervisor_lost(http: TestClient) -> None:
    grant = claimed(http)
    beat = {"supervisor": "box-1", "tasks": [grant["task"]]}

    assert http.post("/tasks/heartbeat", json=beat).json() == {"lost": []}
    http.post("/tokens/T-1/cancel")
    assert http.post("/tasks/heartbeat", json=beat).json() == {"lost": [grant["task"]]}


@pytest.mark.parametrize("tasks", ["1", [True], [-1], [0], [2**70], None])
def test_a_heartbeat_with_wrong_task_ids_is_422(http: TestClient, tasks: object) -> None:
    answer = http.post("/tasks/heartbeat", json={"supervisor": "box-1", "tasks": tasks})

    assert answer.status_code == 422


def test_a_note_goes_into_the_history_without_its_spaces(http: TestClient) -> None:
    grant = claimed(http)

    answer = http.post(f"/tasks/{grant['task']}/note", json={"text": "  tests run  "})

    assert answer.status_code == 204
    history = http.get("/tokens/T-1/history").json()
    notes = [e["data"] for e in history if e["kind"] == "note"]
    assert notes == [{"task": grant["task"], "text": "tests run"}]


@pytest.mark.parametrize("text", ["", "   ", "x" * 501, 5, None])
def test_a_note_that_is_empty_or_too_long_is_422(http: TestClient, text: object) -> None:
    grant = claimed(http)

    assert http.post(f"/tasks/{grant['task']}/note", json={"text": text}).status_code == 422


def test_memory_is_saved_and_its_value_never_enters_the_history(http: TestClient) -> None:
    grant = claimed(http)

    answer = http.put(f"/tasks/{grant['task']}/memory", json={"key": "dev", "value": "s-81"})

    assert answer.status_code == 204
    history = http.get("/tokens/T-1/history").json()
    assert {"state": "coding", "memory_key": "dev"} in [
        e["data"] for e in history if e["kind"] == "memory_set"
    ]
    assert "s-81" not in json.dumps(history)


def test_memory_set_to_null_deletes_the_key(http: TestClient) -> None:
    grant = claimed(http)
    http.put(f"/tasks/{grant['task']}/memory", json={"key": "dev", "value": "s-81"})

    answer = http.put(f"/tasks/{grant['task']}/memory", json={"key": "dev", "value": None})

    assert answer.status_code == 204
    kinds = [e["kind"] for e in http.get("/tokens/T-1/history").json()]
    assert "memory_deleted" in kinds


@pytest.mark.parametrize(
    "body", [{"value": 1}, {"key": "", "value": 1}, {"key": "k" * 129, "value": 1}, {"key": "dev"}]
)
def test_memory_without_a_key_or_a_value_is_422(http: TestClient, body: dict[str, Any]) -> None:
    grant = claimed(http)

    assert http.put(f"/tasks/{grant['task']}/memory", json=body).status_code == 422


def test_a_note_or_memory_on_an_unknown_task_is_404(http: TestClient) -> None:
    claimed(http)

    assert http.post("/tasks/999/note", json={"text": "x"}).status_code == 404
    assert http.put("/tasks/999/memory", json={"key": "k", "value": 1}).status_code == 404
    assert http.put(f"/tasks/{2**70}/memory", json={"key": "k", "value": 1}).status_code == 404


# ── fallbacks: an engine error the API does not name answers with one sentence ──


def test_an_unnamed_engine_error_is_500_with_one_sentence(
    http: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("secret")

    monkeypatch.setattr(engine, "claim", boom)
    bare = TestClient(http.app, raise_server_exceptions=False)

    answer = bare.post("/tasks/claim", json={"supervisor": "box-1", "states": ["coding"]})

    assert answer.status_code == 500
    assert answer.json() == {"error": "the request failed unexpectedly"}


def test_a_database_outage_is_503_with_one_sentence(
    http: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise OperationalError("select 1", {}, Exception("the database is gone"))

    monkeypatch.setattr(engine, "claim", boom)
    bare = TestClient(http.app, raise_server_exceptions=False)

    answer = bare.post("/tasks/claim", json={"supervisor": "box-1", "states": ["coding"]})

    assert answer.status_code == 503
    assert answer.json() == {"error": "the database cannot take this request now; try again"}
