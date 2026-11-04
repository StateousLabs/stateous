"""`waiting_stop`, on histories made by hand: which tokens wait for the world, and which not."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from stateous import HistoryEntry, Token
from stateous.tryout.stops import waiting_stop

AT = datetime(2026, 11, 4, tzinfo=UTC)
DOCUMENT: dict[str, Any] = {
    "states": {"work": {"emits": ["done"]}},
    "transitions": [
        {"from": "__start__", "to": "work"},
        {"from": "work", "to": "__end__", "on": "done"},
        {"from": "work", "to": "__end__", "on": "error"},
    ],
}


def token(state: str = "work", status: str = "active") -> Token:
    return Token("T-1", state, status, 1, {}, AT)


def entries(*kinds: tuple[str, dict[str, Any]]) -> list[HistoryEntry]:
    return [HistoryEntry(seq, AT, kind, data) for seq, (kind, data) in enumerate(kinds, 1)]


def stop(history: list[HistoryEntry], *, state: str = "work", status: str = "active") -> Any:
    return waiting_stop(DOCUMENT, {"work"}, [token(state, status)], {}, {"T-1": history})


def test_a_paused_token_does_not_stop() -> None:
    assert stop(entries(("task_waiting", {})), status="paused") is None


def test_a_token_at_start_or_end_does_not_stop() -> None:
    assert stop(entries(("task_waiting", {})), state="__end__") is None


def test_a_state_before_its_task_does_not_stop() -> None:
    assert stop(entries(("entered", {"state": "work"}))) is None


def test_a_queued_task_of_a_served_state_does_not_stop() -> None:
    assert stop(entries(("entered", {"state": "work"}), ("task_queued", {}))) is None


def test_a_waiting_task_without_entered_still_stops() -> None:
    found = stop(entries(("task_waiting", {})))
    assert found.reason == "waits_outside"
    assert "(done)" in found.sentence


def test_a_timer_that_fired_is_not_a_timer_wait() -> None:
    history = entries(
        ("entered", {"state": "work"}),
        ("task_waiting", {}),
        ("timer_set", {"event": "done", "due_at": "x"}),
        ("timer_fired", {"event": "done"}),
    )
    assert stop(history).reason == "waits_outside"


def test_a_timeout_timer_is_named() -> None:
    history = entries(
        ("entered", {"state": "work"}),
        ("timer_set", {"event": "timeout", "due_at": "2026-11-05T00:00:00+00:00"}),
        ("task_queued", {}),
        ("task_waiting", {}),
    )
    found = stop(history)
    assert found.reason == "waits_timer"
    assert "timeout at 2026-11-05T00:00:00+00:00" in found.sentence
