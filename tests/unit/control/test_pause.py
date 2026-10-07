"""Pause, resume and cancel: a paused token does not move, and holds its events."""

from datetime import UTC, datetime

import pytest

from stateous.control.engine import Engine, EventNotAllowed, TokenNotActive
from stateous.model.wire import Outcome, Schedule, TaskGrant

from .support import TESTS_FAILED, TESTS_PASSED, TICKET, Clock, kinds, new_engine


def started(clock: Clock | None = None) -> Engine:
    engine = new_engine(clock=clock)
    engine.start("T-1", TICKET)
    return engine


def claimed(engine: Engine) -> TaskGrant:
    grant = engine.claim("box-1", ["coding", "testing", "triage"])
    assert grant is not None
    return grant


def memory_of(engine: Engine, state: str) -> dict[str, object]:
    with engine.store.transaction(datetime.now(UTC)) as tx:
        return tx.memory("T-1", state)


def held_error(engine: Engine) -> tuple[str, int]:
    """The state and visit of the `error` that the last ledger entry holds."""
    held = engine.history("T-1")[-1]
    assert (held.kind, held.data["event"], held.data["payload"]) == (
        "event_deferred",
        "error",
        None,
    )
    return held.data["state"], held.data["visit"]


# ── pausing ──────────────────────────────────────────────────────────────


def test_a_paused_token_does_not_move_and_its_events_are_held() -> None:
    engine = started()
    engine.pause("T-1")

    fired = engine.fire("T-1", "done", None)

    assert (fired.token.state, fired.token.status) == ("coding", "paused")
    held = engine.history("T-1")[-1]
    assert (held.kind, held.data) == ("event_deferred", {"event": "done", "payload": None})


def test_only_an_active_token_can_be_paused() -> None:
    engine = started()
    engine.pause("T-1")

    with pytest.raises(TokenNotActive, match="only an active token"):
        engine.pause("T-1")


def test_a_paused_tokens_queued_task_is_not_handed_out() -> None:
    engine = started()
    engine.pause("T-1")

    assert engine.claim("box-1", ["coding"]) is None
    engine.resume("T-1")
    assert engine.claim("box-1", ["coding"]) is not None


def test_a_running_task_finishes_and_its_outcome_is_held() -> None:
    engine = started()
    grant = claimed(engine)
    engine.pause("T-1")

    fired = engine.report(grant.task, Outcome(kind="event", event="done"), supervisor="box-1")

    assert (fired.token.state, fired.token.status) == ("coding", "paused")
    assert engine.heartbeat("box-1", [grant.task]) == [grant.task]  # the work is done
    assert engine.resume("T-1").state == "testing"
    assert "event_replayed" in kinds(engine, "T-1")


# ── resuming ─────────────────────────────────────────────────────────────


def test_resuming_applies_the_held_events_in_order() -> None:
    engine = started()
    engine.pause("T-1")
    engine.fire("T-1", "done", None)
    engine.fire("T-1", "passed", TESTS_PASSED)

    token = engine.resume("T-1")

    assert (token.state, token.status) == ("testing", "completed")
    assert kinds(engine, "T-1").count("event_replayed") == 2


def test_a_held_event_that_no_longer_fits_is_dropped_and_recorded() -> None:
    engine = started()
    engine.pause("T-1")
    engine.fire("T-1", "done", None)
    engine.fire("T-1", "done", None)

    assert engine.resume("T-1").state == "testing"

    dropped = [e for e in engine.history("T-1") if e.kind == "event_dropped"]
    assert [e.data for e in dropped] == [
        {"event": "done", "reason": "no transition for 'done' from 'testing'"}
    ]


def test_a_redelivered_event_is_still_a_duplicate_while_paused() -> None:
    engine = started()
    engine.pause("T-1")
    engine.fire("T-1", "done", None, key="delivery-1")

    again = engine.fire("T-1", "done", None, key="delivery-1")

    assert again.duplicate
    token = engine.resume("T-1")
    assert (token.state, token.visit) == ("testing", 1)
    assert kinds(engine, "T-1").count("event_replayed") == 1

    redelivered = engine.fire("T-1", "done", None, key="delivery-1")

    assert redelivered.duplicate
    accepted = [
        e for e in engine.history("T-1") if e.kind == "event_accepted" and e.data["event"] == "done"
    ]
    assert len(accepted) == 1
    assert (redelivered.token.state, redelivered.token.visit) == ("testing", 1)


def test_only_a_paused_token_can_be_resumed() -> None:
    with pytest.raises(TokenNotActive, match="only a paused token"):
        started().resume("T-1")


def test_a_paused_tokens_timeout_does_not_fire_and_the_paused_time_is_added() -> None:
    clock = Clock()
    engine = started(clock)  # coding times out at 15:00
    clock.advance(hours=5)
    engine.pause("T-1")
    clock.advance(hours=10)
    assert engine.tick() == 0

    engine.resume("T-1")  # paused for 10 hours: it now times out at 01:00
    clock.advance(minutes=59)
    assert engine.tick() == 0
    clock.advance(minutes=1)
    assert engine.tick() == 1

    assert engine.get("T-1").state == "triage"


def test_a_timer_set_while_paused_moves_only_by_the_pause_after_it_was_set() -> None:
    clock = Clock()
    engine = started(clock)
    grant = claimed(engine)
    engine.pause("T-1")
    clock.advance(hours=1)
    timer = Schedule(event="stuck", payload=None, after="10m")
    outcome = Outcome(kind="waiting", schedules=(timer,))
    engine.report(grant.task, outcome, supervisor="box-1")  # due 10:10
    clock.advance(hours=1)

    engine.resume("T-1")  # one hour of the pause came after it was set: due 11:10
    clock.advance(minutes=9)
    assert engine.tick() == 0
    clock.advance(minutes=1)
    assert engine.tick() == 1

    assert engine.get("T-1").status == "completed"


def test_retries_used_up_while_paused_hold_error_until_resume() -> None:
    engine = started()
    for _ in range(2):
        engine.report(claimed(engine).task, Outcome(kind="failed", error="x"), supervisor="box-1")
    last = claimed(engine)
    engine.pause("T-1")

    engine.report(last.task, Outcome(kind="failed", error="x"), supervisor="box-1")

    assert engine.get("T-1").state == "coding"
    assert held_error(engine) == ("coding", 1)
    assert engine.resume("T-1").state == "triage"  # coding --error--> triage


def test_a_lease_that_ends_while_paused_holds_error_until_resume() -> None:
    clock = Clock()
    engine = started(clock)
    for _ in range(2):
        engine.report(claimed(engine).task, Outcome(kind="failed", error="x"), supervisor="box-1")
    claimed(engine)  # the last attempt
    engine.pause("T-1")

    clock.advance(minutes=2, seconds=1)
    assert engine.tick() == 1

    assert engine.get("T-1").state == "coding"
    assert held_error(engine) == ("coding", 1)
    assert engine.resume("T-1").state == "triage"  # coding --error--> triage


def test_a_final_failure_with_no_error_transition_holds_until_resume_then_fails() -> None:
    engine = started()
    engine.fire("T-1", "done", None)  # testing: retries=0, no transition on error
    grant = claimed(engine)
    engine.pause("T-1")

    engine.report(grant.task, Outcome(kind="failed", error="x"), supervisor="box-1")

    token = engine.get("T-1")
    assert (token.state, token.status) == ("testing", "paused")
    assert held_error(engine) == ("testing", 1)

    token = engine.resume("T-1")
    assert token.status == "failed"
    assert "testing" in engine.history("T-1")[-1].data["reason"]


# ── cancelling ───────────────────────────────────────────────────────────


def test_a_cancelled_token_stops_its_task_and_forgets_its_memory() -> None:
    engine = started()
    grant = claimed(engine)
    engine.set_memory(grant.task, "dev", "s-1")

    token = engine.cancel("T-1")

    assert (token.state, token.status) == ("coding", "cancelled")
    assert kinds(engine, "T-1")[-2:] == ["task_cancelled", "cancelled"]
    assert engine.heartbeat("box-1", [grant.task]) == [grant.task]
    assert memory_of(engine, "coding") == {}


def test_a_paused_token_can_be_cancelled_and_drops_what_it_held() -> None:
    engine = started()
    engine.pause("T-1")
    engine.fire("T-1", "done", None)

    engine.cancel("T-1")

    dropped = [e for e in engine.history("T-1") if e.kind == "event_dropped"]
    assert [e.data for e in dropped] == [{"event": "done", "reason": "the token was cancelled"}]


def test_a_token_that_ended_cannot_be_paused_or_cancelled() -> None:
    engine = started()
    engine.fire("T-1", "stuck", None)

    with pytest.raises(TokenNotActive, match="completed"):
        engine.pause("T-1")
    with pytest.raises(TokenNotActive, match="only an active or paused token"):
        engine.cancel("T-1")


def test_a_cancelled_tokens_timers_do_not_fire() -> None:
    clock = Clock()
    engine = started(clock)
    engine.cancel("T-1")

    clock.advance(hours=6)

    assert engine.tick() == 0


def test_live_tokens_counts_paused_tokens_too() -> None:
    engine = started()
    engine.start("T-2", TICKET)
    engine.start("T-3", TICKET)
    engine.pause("T-2")
    engine.cancel("T-3")

    assert engine.live_tokens() == 2


# ── what a paused token holds from its tasks ─────────────────────────────


def guarded() -> dict[str, object]:
    """One state `a` that emits `go`. `go` leads to END only when ctx['ok'] holds;
    there is no otherwise."""
    return {
        "stateous": 1,
        "workflow": "guarded",
        "context": "Flags",
        "states": {"a": {"emits": ["go"]}},
        "events": {"go": {}},
        "transitions": [
            {"from": "__start__", "to": "a"},
            {"from": "a", "to": "__end__", "on": "go", "when": "ctx['ok'] == true"},
        ],
        "schemas": {
            "Flags": {
                "type": "object",
                "properties": {"ok": {"type": "boolean", "default": False}},
            }
        },
    }


def test_a_report_while_paused_is_refused_as_when_active() -> None:
    engine = new_engine(guarded())
    engine.start("T-1", {})
    grant = engine.claim("box-1", ["a"])
    assert grant is not None
    engine.pause("T-1")

    with pytest.raises(EventNotAllowed, match="no guard matched 'go'"):
        engine.report(grant.task, Outcome(kind="event", event="go"), supervisor="box-1")

    assert engine.history("T-1")[-1].kind == "event_rejected"
    assert engine.heartbeat("box-1", [grant.task]) == []  # still held
    assert "event_deferred" not in kinds(engine, "T-1")


def test_a_held_error_from_a_state_the_token_left_is_dropped_on_resume() -> None:
    engine = started()
    engine.fire("T-1", "done", None)  # testing: retries=0
    grant = claimed(engine)
    engine.pause("T-1")
    engine.fire("T-1", "failed", TESTS_FAILED)  # held, from outside
    engine.report(grant.task, Outcome(kind="failed", error="x"), supervisor="box-1")

    held = engine.history("T-1")[-1]
    assert held.data == {
        "event": "error", "payload": None, "state": "testing", "visit": 1, "task": grant.task,
    }  # fmt: skip

    token = engine.resume("T-1")

    assert (token.state, token.status) == ("coding", "active")
    assert engine.claim("box-1", ["coding"]) is not None  # its new task
    dropped = [e.data for e in engine.history("T-1") if e.kind == "event_dropped"]
    assert dropped == [{"event": "error", "reason": "the token left the state it came from"}]


def test_a_held_outcome_records_the_state_and_task_it_came_from() -> None:
    engine = started()
    grant = claimed(engine)
    engine.pause("T-1")

    engine.report(grant.task, Outcome(kind="event", event="done"), supervisor="box-1")

    deferred = next(e for e in engine.history("T-1") if e.kind == "event_deferred")
    assert deferred.data == {
        "event": "done", "payload": None, "state": "coding", "visit": 1, "task": grant.task,
    }  # fmt: skip


# ── a held outcome keeps its context changes and timers with it ──────────


def test_a_held_outcome_applies_its_context_changes_and_timers_only_on_resume() -> None:
    clock = Clock()
    engine = started(clock)
    grant = claimed(engine)
    engine.pause("T-1")
    timer = Schedule(event="stuck", payload=None, after="10m")
    outcome = Outcome(kind="event", event="done", context={"rejected": 1}, schedules=(timer,))

    engine.report(grant.task, outcome, supervisor="box-1")

    token = engine.get("T-1")
    assert (token.state, token.context["rejected"]) == ("coding", 0)
    assert "context_changed" not in kinds(engine, "T-1")
    assert [e.data["event"] for e in engine.history("T-1") if e.kind == "timer_set"] == ["timeout"]

    clock.advance(hours=1)
    token = engine.resume("T-1")

    assert (token.state, token.context["rejected"]) == ("testing", 1)
    after = kinds(engine, "T-1")[kinds(engine, "T-1").index("resumed") :]
    assert after[:3] == ["resumed", "event_replayed", "context_changed"]
    stuck = [
        e for e in engine.history("T-1") if e.kind == "timer_set" and e.data["event"] == "stuck"
    ]
    assert [e.data["due_at"] for e in stuck] == ["2026-10-06T10:10:00Z"]  # from the resume


def test_a_dropped_held_outcome_applies_no_context_changes_and_sets_no_timers() -> None:
    engine = started()
    grant = claimed(engine)
    engine.pause("T-1")
    engine.fire("T-1", "done", None)  # held first: it moves the token on resume
    timer = Schedule(event="stuck", payload=None, after="10m")
    outcome = Outcome(kind="event", event="stuck", context={"rejected": 1}, schedules=(timer,))
    engine.report(grant.task, outcome, supervisor="box-1")

    token = engine.resume("T-1")

    assert (token.state, token.status, token.context["rejected"]) == ("testing", "active", 0)
    assert "context_changed" not in kinds(engine, "T-1")
    assert not [
        e for e in engine.history("T-1") if e.kind == "timer_set" and e.data["event"] == "stuck"
    ]
    dropped = [e.data for e in engine.history("T-1") if e.kind == "event_dropped"]
    assert dropped == [{"event": "stuck", "reason": "the token left the state it came from"}]


def test_the_history_shows_a_held_outcome_once() -> None:
    engine = started()
    grant = claimed(engine)
    engine.pause("T-1")
    timer = Schedule(event="stuck", payload=None, after="10m")
    outcome = Outcome(kind="event", event="done", context={"rejected": 1}, schedules=(timer,))
    engine.report(grant.task, outcome, supervisor="box-1")

    engine.resume("T-1")

    history = engine.history("T-1")
    deferred = [e.data for e in history if e.kind == "event_deferred"]
    assert deferred == [
        {
            "event": "done", "payload": None, "state": "coding", "visit": 1, "task": grant.task,
            "context": {"rejected": 1},
            "schedules": [{"event": "stuck", "after": "10m"}],
        }
    ]  # fmt: skip
    assert [e.kind for e in history].count("event_replayed") == 1
    assert [e.kind for e in history].count("context_changed") == 1
