"""`TryReport`: the report of a try-out, as text and as JSON."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from stateous import HistoryEntry, Token
from stateous.tryout.report import PAGE_OF, Stop, TryReport
from stateous.tryout.watch import Step

AT = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def report(page: str | None = "concepts/timers.md") -> TryReport:
    step = Step(
        3,
        "T-1",
        "coding",
        1,
        AT,
        AT + timedelta(seconds=12),
        "event done",
        ["read the ticket", "wrote the code"],
    )
    running = Step(4, "T-1", "review", 1, AT + timedelta(seconds=12))
    token = Token("T-1", "review", "active", 2, {"title": "x"}, AT)
    history = [
        HistoryEntry(1, AT, "started", {"context": {"title": "x"}}),
        HistoryEntry(2, AT, "entered", {"state": "coding"}),
    ]
    stop = Stop("waits_timer", "T-1", "review", "T-1 waits at review for the timer of nudge.", page)
    return TryReport(
        stop, [step, running], [token], {"T-1": history}, ["a=b"], ["c=d"], Path("try.db")
    )


def test_the_first_line_is_the_stop_sentence() -> None:
    assert report().to_text().splitlines()[0] == "T-1 waits at review for the timer of nudge."


def test_each_step_has_a_line_and_its_notes_under_it() -> None:
    lines = report().to_text().splitlines()
    index = lines.index("T-1  coding  attempt 1  12.0 s  event done")
    assert lines[index + 1 : index + 3] == ["    note: read the ticket", "    note: wrote the code"]
    assert "T-1  review  attempt 1  running" in lines


def test_the_history_of_each_token_follows() -> None:
    lines = report().to_text().splitlines()
    index = lines.index("history of T-1:")
    assert lines[index + 1].split()[:2] == ["1", "started"]
    assert lines[index + 2].split()[:2] == ["2", "entered"]


def test_the_tokens_fires_database_and_page_are_named() -> None:
    text = report().to_text()
    assert 'T-1  active at review  context {"title": "x"}' in text
    assert "fires used: a=b" in text
    assert "fires not used: c=d" in text
    assert "database: try.db" in text
    assert text.splitlines()[-1] == "read: references/concepts/timers.md"


def test_no_page_no_read_line() -> None:
    assert "read:" not in report(page=None).to_text()


def test_json_has_every_part_and_dumps_as_it_is() -> None:
    data = report().to_json()
    assert set(data) == {
        "stop",
        "steps",
        "tokens",
        "histories",
        "fires_used",
        "fires_unused",
        "database",
    }
    assert data["stop"]["reason"] == "waits_timer"
    assert data["steps"][0]["seconds"] == 12.0
    assert data["steps"][1]["ended"] is None
    assert data["histories"]["T-1"][0]["kind"] == "started"
    json.dumps(data)


def test_every_stop_reason_has_a_page_entry() -> None:
    assert set(PAGE_OF) == {
        "completed", "failed", "cancelled", "no_routine",
        "waits_outside", "waits_timer", "quiet", "time_limit",
    }  # fmt: skip
    assert PAGE_OF["completed"] is None
