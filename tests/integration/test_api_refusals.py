"""Every refusal of the HTTP API has a stable `code` beside its sentence
(docs/design.md, section 5.4). Clients read the code, never the sentence."""

from typing import Any

import pytest
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException
from starlette.testclient import TestClient

from stateous.control.api import MAX_BODY
from stateous.control.engine import Engine

BIG_TASK = 2**70


def prepared(http: TestClient) -> int:
    """T-1 in coding, its task claimed by box-1; T-2 cancelled. Returns the task id."""
    for token in ("T-1", "T-2"):
        started = http.post("/tokens", json={"token": token, "context": {"repo": "acme/api"}})
        assert started.status_code == 201, started.text
    assert http.post("/tokens/T-2/cancel").status_code == 200
    grant = http.post("/tasks/claim", json={"supervisor": "box-1", "states": ["coding"]})
    assert grant.status_code == 200, grant.text
    return grant.json()["task"]


TESTS = {"payload": {"passed": 1, "failed": 0}}

CASES: list[tuple[str, str, dict[str, Any], int, str]] = [
    ("GET", "/tokens/NOPE", {}, 404, "token_not_found"),
    ("GET", "/tokens/NOPE/history", {}, 404, "token_not_found"),
    ("POST", "/tasks/999999/note", {"json": {"text": "x"}}, 404, "task_not_found"),
    ("POST", f"/tasks/{BIG_TASK}/note", {"json": {"text": "x"}}, 404, "task_not_found"),
    ("GET", "/nothing", {}, 404, "not_found"),
    ("GET", "/tokens/T-1/pause", {}, 405, "method_not_allowed"),
    ("POST", "/tokens", {"content": b"{"}, 400, "bad_request"),
    ("POST", "/tokens", {"content": b'{"token": NaN}'}, 400, "bad_request"),
    ("GET", "/tokens?limit=0", {}, 422, "invalid_argument"),
    ("GET", "/tokens?status=asleep", {}, 422, "invalid_argument"),
    ("GET", "/tokens?parent=" + "x" * 129, {}, 422, "invalid_argument"),
    ("POST", "/tasks/claim", {"json": {"supervisor": "", "states": []}}, 422, "invalid_argument"),
    (
        "POST",
        "/tasks/claim",
        {"json": {"supervisor": "box-1", "states": [], "request": "no spaces"}},
        422,
        "invalid_argument",
    ),
    (
        "POST",
        "/tasks/{task}/note",
        {"json": {"text": "x", "attempt": 0}},
        422,
        "invalid_argument",
    ),
    ("POST", "/tokens", {"json": {"token": "T-3", "context": {}}}, 422, "invalid_context"),
    ("POST", "/tokens", {"json": {"token": "T-3", "context": [1]}}, 422, "invalid_context"),
    ("POST", "/tasks/{task}/note", {"json": {"text": " "}}, 422, "invalid_payload"),
    (
        "PUT",
        "/tasks/{task}/memory",
        {"json": {"key": "k" * 129, "value": 1}},
        422,
        "invalid_payload",
    ),
    (
        "POST",
        "/tokens/T-1/events/done",
        {"json": {}, "headers": {"Idempotency-Key": "k" * 257}},
        422,
        "invalid_payload",
    ),
    ("POST", "/tokens/T-1/events/done", {"json": {"payload": 1}}, 422, "invalid_payload"),
    ("POST", "/tokens", {"json": {"token": "bad id"}}, 422, "invalid_token_id"),
    ("POST", "/tokens", {"json": {"token": 7}}, 422, "invalid_token_id"),
    ("POST", "/tokens", {"content": b"[1, 2]"}, 422, "invalid_body"),
    ("POST", "/tasks/{task}/report", {"json": {"kind": "nope"}}, 422, "invalid_body"),
    ("PUT", "/tasks/{task}/memory", {"json": {"key": "k"}}, 422, "invalid_body"),
    ("POST", "/tasks/claim", {"json": {"supervisor": "box-1"}}, 422, "invalid_body"),
    ("POST", "/tasks/heartbeat", {"json": {"supervisor": "box-1"}}, 422, "invalid_body"),
    ("POST", "/tokens/T-1/events/passed", {"json": TESTS}, 409, "event_not_allowed"),
    ("POST", "/tokens/T-2/events/done", {"json": {}}, 409, "token_not_active"),
    (
        "POST",
        "/tasks/{task}/report",
        {"json": {"kind": "event", "event": "done", "supervisor": "box-2"}},
        409,
        "task_not_held",
    ),
]


@pytest.mark.parametrize(
    ("method", "path", "send", "status", "code"),
    CASES,
    ids=[f"{code}:{method} {path[:40]}" for method, path, _, _, code in CASES],
)
def test_a_refusal_has_the_code_of_its_row(
    http: TestClient, method: str, path: str, send: dict[str, Any], status: int, code: str
) -> None:
    task = prepared(http)
    headers = {"content-type": "application/json", **send.get("headers", {})}
    answer = http.request(
        method,
        path.replace("{task}", str(task)),
        json=send.get("json"),
        content=send.get("content"),
        headers=headers,
    )

    assert answer.status_code == status, answer.text
    body = answer.json()
    assert set(body) == {"error", "code"}
    assert body["code"] == code
    assert isinstance(body["error"], str) and body["error"]


def test_a_note_after_the_task_ended_is_task_not_held(http: TestClient) -> None:
    task = prepared(http)
    reported = http.post(f"/tasks/{task}/report", json={"kind": "event", "event": "done"})
    assert reported.status_code == 200, reported.text

    answer = http.post(f"/tasks/{task}/note", json={"text": "late"})

    assert (answer.status_code, answer.json()["code"]) == (409, "task_not_held")


def test_a_request_for_an_old_attempt_is_attempt_over(http: TestClient) -> None:
    task = prepared(http)
    failed = {"kind": "failed", "error": "x", "supervisor": "box-1", "attempt": 1}
    assert http.post(f"/tasks/{task}/report", json=failed).status_code == 200
    again = http.post("/tasks/claim", json={"supervisor": "box-1", "states": ["coding"]})
    assert again.json()["attempt"] == 2

    late = {"kind": "event", "event": "done", "supervisor": "box-1", "attempt": 1}
    report = http.post(f"/tasks/{task}/report", json=late)
    note = http.post(f"/tasks/{task}/note", json={"text": "late", "attempt": 1})

    assert (report.status_code, report.json()["code"]) == (409, "attempt_over")
    assert (note.status_code, note.json()["code"]) == (409, "attempt_over")


def test_a_body_larger_than_the_limit_is_bad_request_413(http: TestClient) -> None:
    answer = http.post(
        "/tokens", content=b" " * (MAX_BODY + 1), headers={"content-type": "application/json"}
    )

    assert answer.status_code == 413
    assert answer.json()["code"] == "bad_request"


@pytest.mark.parametrize(
    ("raised", "status", "code"),
    [
        (OperationalError("select 1", {}, Exception("gone")), 503, "database_busy"),
        (RuntimeError("secret"), 500, "unexpected"),
        (HTTPException(418), 418, "bad_request"),
        (HTTPException(502), 502, "unexpected"),
    ],
)
def test_a_failure_that_no_route_names_has_a_code(
    http: TestClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
    status: int,
    code: str,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise raised

    monkeypatch.setattr(engine, "claim", boom)
    bare = TestClient(http.app, raise_server_exceptions=False)

    answer = bare.post("/tasks/claim", json={"supervisor": "box-1", "states": ["coding"]})

    assert answer.status_code == status
    assert answer.json()["code"] == code
