"""What a routine gets as `ctx` (design section 5.2, contract section 9)."""

import math
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from stateous import Context, Occurrence
from stateous.model.context import Memory
from stateous.model.wire import EventRecord, Schedule, TaskGrant

GRANT = TaskGrant(
    task=7,
    token="TICKET-1",
    state="testing",
    visit=2,
    attempt=1,
    context={"repo": "acme/api", "pr": 88},
    memory={"dev": "session-1"},
    entered_by=EventRecord("failed", {"passed": 3, "failed": 1}),
)


class Calls:
    """What the context sent at once: memory saves and notes, in order."""

    def __init__(self) -> None:
        self.saved: list[tuple[str, Any]] = []
        self.notes: list[str] = []

    def save(self, key: str, value: Any) -> None:
        self.saved.append((key, value))

    def note(self, text: str) -> None:
        self.notes.append(text)


def context(calls: Calls) -> Context[Any]:
    return Context[Any](GRANT, save_memory=calls.save, note=calls.note)


# ── the task's facts ──────────────────────────────────────────────────────


def test_a_context_carries_the_facts_of_its_task() -> None:
    ctx = context(Calls())

    assert (ctx.token, ctx.task, ctx.visit, ctx.attempt) == ("TICKET-1", 7, 2, 1)
    assert ctx.entered_by == EventRecord("failed", {"passed": 3, "failed": 1})
    assert dict(ctx.memory) == {"dev": "session-1"}


# ── the token's context ───────────────────────────────────────────────────


def test_reading_and_writing_the_tokens_context() -> None:
    ctx = context(Calls())

    ctx["pr"] = 89
    ctx["branch"] = "fix-1"

    assert (ctx["repo"], ctx["pr"], ctx["branch"]) == ("acme/api", 89, "fix-1")
    assert ctx.get("missing") is None
    assert ctx.get("missing", 3) == 3
    assert ctx.writes() == {"pr": 89, "branch": "fix-1"}


def test_reading_a_missing_key_names_it() -> None:
    with pytest.raises(KeyError, match="brief"):
        _ = context(Calls())["brief"]


@pytest.mark.parametrize("value", [datetime.now(UTC), {1, 2}, math.nan, object()])
def test_a_write_that_is_not_json_is_refused_at_once(value: Any) -> None:
    ctx = context(Calls())

    with pytest.raises(TypeError, match=r"ctx\['pr'\] must be JSON"):
        ctx["pr"] = value
    assert ctx.writes() == {}
    assert ctx["pr"] == 88


def test_writes_is_a_copy() -> None:
    ctx = context(Calls())
    ctx["pr"] = 1

    ctx.writes()["pr"] = 2

    assert ctx.writes() == {"pr": 1}


# ── notes ─────────────────────────────────────────────────────────────────


def test_a_note_is_sent_at_once_without_surrounding_space() -> None:
    calls = Calls()

    context(calls).note("  tests ran: 41 passed  ")

    assert calls.notes == ["tests ran: 41 passed"]


@pytest.mark.parametrize(
    ("text", "reason"),
    [("", "needs some text"), ("   ", "needs some text"), ("x" * 501, "at most 500 characters")],
)
def test_an_empty_or_long_note_is_refused(text: str, reason: str) -> None:
    calls = Calls()

    with pytest.raises(ValueError, match=reason):
        context(calls).note(text)
    assert calls.notes == []


# ── timers ────────────────────────────────────────────────────────────────


class Nudge(BaseModel):
    who: str


class remind(Occurrence[Nudge]):  # noqa: N801
    name = "remind"


class escalate(Occurrence[None]):  # noqa: N801
    name = "escalate"


def test_a_schedule_is_kept_to_travel_with_the_report() -> None:
    ctx = context(Calls())

    ctx.schedule(remind(Nudge(who="alex")), after="1d")
    ctx.schedule(escalate, after="30m")
    ctx.schedule(escalate(), after="2h")

    assert ctx.schedules() == [
        Schedule("remind", {"who": "alex"}, "1d"),
        Schedule("escalate", None, "30m"),
        Schedule("escalate", None, "2h"),
    ]


def test_an_event_with_a_payload_is_scheduled_as_an_occurrence() -> None:
    ctx = context(Calls())

    with pytest.raises(ValueError, match="'remind' carries a payload"):
        ctx.schedule(remind, after="1d")
    assert ctx.schedules() == []


@pytest.mark.parametrize("after", ["soon", "0s", "1w", "1.5h", "", "3651d", "30s\n"])
def test_a_schedule_needs_a_duration(after: str) -> None:
    ctx = context(Calls())

    with pytest.raises(ValueError, match=r"is not a duration|is too long"):
        ctx.schedule(escalate, after=after)
    assert ctx.schedules() == []


# ── memory ────────────────────────────────────────────────────────────────


def test_memory_is_saved_at_once() -> None:
    calls = Calls()
    ctx = context(calls)

    ctx.memory["review"] = {"session": "s-2", "turns": 3}
    del ctx.memory["dev"]

    assert calls.saved == [("review", {"session": "s-2", "turns": 3}), ("dev", None)]
    assert dict(ctx.memory) == {"review": {"session": "s-2", "turns": 3}}
    assert len(ctx.memory) == 1
    assert "dev" not in ctx.memory


def test_deleting_a_missing_key_saves_nothing() -> None:
    calls = Calls()
    memory = Memory({}, calls.save)

    with pytest.raises(KeyError):
        del memory["nothing"]
    assert calls.saved == []


def test_memory_refuses_what_it_cannot_keep() -> None:
    calls = Calls()
    memory = Memory({}, calls.save)

    with pytest.raises(TypeError, match=r"memory\['when'\] must be JSON"):
        memory["when"] = datetime.now(UTC)
    with pytest.raises(ValueError, match="use `del"):
        memory["dev"] = None
    with pytest.raises(ValueError, match="a memory key is 1 to 128 characters"):
        memory[""] = 1
    with pytest.raises(ValueError, match="a memory key is 1 to 128 characters"):
        memory["k" * 129] = 1
    assert calls.saved == []
    assert dict(memory) == {}


def test_a_failed_save_leaves_memory_as_it_was() -> None:
    def broken(key: str, value: Any) -> None:
        raise ConnectionError("control plane unreachable")

    memory = Memory({"dev": "s-1"}, broken)

    with pytest.raises(ConnectionError):
        memory["dev"] = "s-2"
    assert memory["dev"] == "s-1"


# ── values are copies ─────────────────────────────────────────────────────


def nested_grant() -> TaskGrant:
    return TaskGrant(
        task=1,
        token="T",
        state="s",
        visit=1,
        attempt=1,
        context={"items": [1], "meta": {"a": 1}},
        memory={"seen": [1]},
        entered_by=None,
    )


def test_changing_a_value_read_from_the_context_changes_nothing() -> None:
    ctx = Context[Any](nested_grant(), save_memory=Calls().save, note=Calls().note)

    ctx["items"].append(2)
    ctx.get("meta")["a"] = 2

    assert ctx["items"] == [1]
    assert ctx.get("meta") == {"a": 1}
    assert ctx.writes() == {}


def test_changing_a_value_after_writing_it_changes_nothing() -> None:
    ctx = Context[Any](nested_grant(), save_memory=Calls().save, note=Calls().note)
    items = [1, 2]

    ctx["items"] = items
    items.append(99)
    ctx.writes()["items"].append(3)

    assert ctx["items"] == [1, 2]
    assert ctx.writes() == {"items": [1, 2]}


def test_a_write_is_kept_as_json_keeps_it() -> None:
    ctx = Context[Any](nested_grant(), save_memory=Calls().save, note=Calls().note)

    ctx["pair"] = (1, 2)

    assert ctx["pair"] == [1, 2]
    assert ctx.writes() == {"pair": [1, 2]}


def test_changing_a_value_read_from_memory_changes_nothing() -> None:
    calls = Calls()
    ctx = Context[Any](nested_grant(), save_memory=calls.save, note=calls.note)

    ctx.memory["seen"].append(2)
    later = [3]
    ctx.memory["later"] = later
    later.append(4)

    assert ctx.memory["seen"] == [1]
    assert ctx.memory["later"] == [3]
    assert calls.saved == [("later", [3])]
