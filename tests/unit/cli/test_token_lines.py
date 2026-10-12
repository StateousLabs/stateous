"""How `stateous token` shows a token and its history, and reads JSON arguments."""

from datetime import UTC, datetime
from typing import Any

import pytest

from stateous.cli.errors import CommandError
from stateous.cli.tokens import entry_line, json_object, token_line


def test_a_token_line_names_the_id_the_state_and_the_status() -> None:
    token = {"token": "T-1", "state": "coding", "status": "active", "visit": 1}

    assert token_line(token) == "T-1: coding, active"
    assert token_line({**token, "visit": 2}) == "T-1: coding (visit 2), active"
    assert token_line({**token, "state": "__end__", "status": "completed"}) == (
        "T-1: END, completed"
    )


@pytest.mark.parametrize(
    ("kind", "data", "text"),
    [
        # Every shape below is the real one the engine writes (control/engine.py's
        # `tx.append(...)` calls), not a guessed one.
        ("started", {"context": {"repo": "a"}}, "started"),
        ("started", {"context": {"repo": "a"}, "parent": "T-1"}, "started by T-1"),
        (
            "branch_started",
            {"workflow": "security", "token": "T-1.reviews.1.security"},
            "started the child T-1.reviews.1.security",
        ),
        (
            "cancelled",
            {"reason": "the parent left 'reviews'"},
            "cancelled: the parent left 'reviews'",
        ),
        ("cancelled", {}, "cancelled"),
        ("entered", {"state": "coding", "visit": 2}, "entered coding (visit 2)"),
        ("task_queued", {"task": 4, "state": "coding"}, "task 4 queued for coding"),
        (
            "task_claimed",
            {"task": 4, "supervisor": "box-1", "attempt": 1},
            "box-1 took task 4 (attempt 1)",
        ),
        ("task_waiting", {"task": 4}, "task 4 waits: the work goes on outside"),
        ("task_done", {"task": 4}, "task 4 done"),
        (
            "task_failed",
            {"task": 4, "error": "RuntimeError: no"},
            "task 4 failed: RuntimeError: no",
        ),
        (
            "task_retrying",
            {"task": 4, "attempt": 2},
            "task 4 is tried again (attempt 2)",
        ),
        ("task_cancelled", {"task": 4}, "task 4 cancelled"),
        (
            "event_accepted",
            {"event": "passed", "payload": {"passed": 3}, "state": "testing"},
            'passed in testing: {"passed": 3}',
        ),
        (
            "event_rejected",
            {"event": "passed", "reason": "not allowed"},
            "passed refused: not allowed",
        ),
        ("timer_fired", {"event": "stuck"}, "timer fired: stuck"),
        ("note", {"task": 4, "text": "tests run"}, "tests run"),
        (
            "memory_set",
            {"state": "coding", "memory_key": "dev"},
            "memory set: dev (in coding)",
        ),
        (
            "memory_deleted",
            {"state": "coding", "memory_key": "dev"},
            "memory deleted: dev (in coding)",
        ),
        ("context_changed", {"keys": ["repo", "branch"]}, "context changed: repo, branch"),
        ("paused", {}, "paused"),
        ("resumed", {}, "resumed"),
        ("completed", {}, "completed"),
        ("cancelled", {}, "cancelled"),
        (
            "event_deferred",
            {"key": None, "event": "done", "payload": None},
            "done held while paused",
        ),
        ("event_replayed", {"event": "done"}, "done applied on resume"),
        (
            "event_dropped",
            {"event": "done", "reason": "the token left that state"},
            "done dropped on resume: the token left that state",
        ),
        ("failed", {"reason": "no transition on timeout"}, "failed: no transition on timeout"),
        ("something_new", {"a": 1}, 'something_new {"a": 1}'),
    ],
)
def test_an_entry_line_says_what_happened(kind: str, data: dict[str, Any], text: str) -> None:
    line = entry_line({"seq": 7, "at": "2026-10-06T10:00:00Z", "kind": kind, "data": data})

    assert line.endswith(f"  #7  {text}")
    assert line.isascii()


def test_an_entry_line_shows_local_time() -> None:
    local = datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC).astimezone().strftime("%H:%M:%S")

    line = entry_line({"seq": 1, "at": "2026-10-06T10:00:00Z", "kind": "paused", "data": {}})

    assert line == f"{local}  #1  paused"


def test_a_timer_set_entry_shows_the_local_due_time() -> None:
    """`timer_set`'s `due_at` (control/engine.py's `_set_timer`) is shown in local
    time, with the date, since a timer can fire on a different day."""
    due = datetime(2026, 10, 6, 16, 0, 0, tzinfo=UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")

    line = entry_line(
        {
            "seq": 2,
            "at": "2026-10-06T10:00:00Z",
            "kind": "timer_set",
            "data": {"event": "stuck", "due_at": "2026-10-06T16:00:00Z"},
        }
    )

    assert line.endswith(f"  #2  timer set: stuck at {due}")


def test_an_entry_with_missing_data_does_not_fail() -> None:
    line = entry_line({"seq": 1, "at": "2026-10-06T10:00:00Z", "kind": "task_claimed", "data": {}})

    assert line.endswith("? took task ? (attempt ?)")


def test_a_json_object_argument_is_read() -> None:
    assert json_object('{"repo": "acme/api"}', "CONTEXT_JSON") == {"repo": "acme/api"}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{bad", "CONTEXT_JSON is not valid JSON"),
        ("", "CONTEXT_JSON is not valid JSON"),
        ('{"n": NaN}', "CONTEXT_JSON is not valid JSON"),
        ("[1]", "CONTEXT_JSON must be a JSON object"),
    ],
)
def test_a_json_argument_that_is_wrong_is_one_sentence(text: str, message: str) -> None:
    with pytest.raises(CommandError) as caught:
        json_object(text, "CONTEXT_JSON")

    assert message in str(caught.value)
    assert "\n" not in str(caught.value)
