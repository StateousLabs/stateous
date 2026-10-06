"""The clock: timeouts and timers (one mechanism), and leases that end."""

import pytest

from stateous.control.engine import Engine, InvalidPayload, TaskNotHeld
from stateous.model.wire import Outcome, Schedule, TaskGrant

from .support import TICKET, Clock, kinds, new_engine


def started(clock: Clock) -> Engine:
    engine = new_engine(clock=clock)
    engine.start("T-1", TICKET)
    return engine


def claimed(engine: Engine, supervisor: str = "box-1") -> TaskGrant:
    grant = engine.claim(supervisor, ["coding", "testing", "triage"])
    assert grant is not None
    return grant


def waiting_with_a_timer(event: str = "stuck", after: str = "10m") -> Outcome:
    return Outcome(kind="waiting", schedules=(Schedule(event=event, payload=None, after=after),))


# ── timeouts ─────────────────────────────────────────────────────────────


def test_nothing_fires_before_the_timeout() -> None:
    clock = Clock()
    engine = started(clock)

    clock.advance(hours=5, minutes=59)

    assert engine.tick() == 0
    assert engine.get("T-1").state == "coding"


def test_an_elapsed_timeout_fires_and_entering_again_sets_it_again() -> None:
    clock = Clock()
    engine = started(clock)

    clock.advance(hours=6)
    assert engine.tick() == 1
    assert engine.get("T-1").state == "triage"  # coding --timeout--> triage
    assert "timer_fired" in kinds(engine, "T-1")

    engine.fire("T-1", "triaged", None)
    clock.advance(hours=6)
    assert engine.tick() == 1
    assert engine.get("T-1").state == "triage"


def test_a_timeout_with_no_transition_fails_the_token() -> None:
    clock = Clock()
    engine = started(clock)
    engine.fire("T-1", "done", None)  # testing: 1h, no transition on timeout

    clock.advance(hours=1)
    engine.tick()

    token = engine.get("T-1")
    assert (token.state, token.status) == ("testing", "failed")
    assert "'testing' timed out" in engine.history("T-1")[-1].data["reason"]


def test_a_token_that_ended_has_no_timeout() -> None:
    clock = Clock()
    engine = started(clock)
    engine.fire("T-1", "stuck", None)

    clock.advance(hours=6)

    assert engine.tick() == 0
    assert engine.get("T-1").status == "completed"


# ── timers a routine sets ───────────────────────────────────────────────


def test_a_routines_timer_fires_its_event_once_when_it_is_due() -> None:
    clock = Clock()
    engine = started(clock)
    grant = claimed(engine)
    engine.report(grant.task, waiting_with_a_timer(), supervisor="box-1")
    timer_set = [e for e in engine.history("T-1") if e.kind == "timer_set"][-1]
    assert timer_set.data == {"event": "stuck", "due_at": "2026-10-06T09:10:00Z"}

    clock.advance(minutes=9)
    assert engine.tick() == 0
    clock.advance(minutes=1)
    assert engine.tick() == 1
    assert engine.tick() == 0

    assert engine.get("T-1").status == "completed"  # coding --stuck--> END


def test_a_timer_is_cancelled_when_the_token_leaves_its_state() -> None:
    clock = Clock()
    engine = started(clock)
    grant = claimed(engine)
    timer = Schedule(event="stuck", payload=None, after="10m")
    outcome = Outcome(kind="event", event="done", schedules=(timer,))
    engine.report(grant.task, outcome, supervisor="box-1")

    clock.advance(minutes=10)

    assert engine.tick() == 0
    assert engine.get("T-1").state == "testing"


@pytest.mark.parametrize("event", ["dnoe", "timeout"])
def test_a_timer_must_name_an_event_of_the_workflow(event: str) -> None:
    engine = started(Clock())
    grant = claimed(engine)

    with pytest.raises(InvalidPayload, match="not an event"):
        engine.report(grant.task, waiting_with_a_timer(event), supervisor="box-1")


def test_a_timer_must_have_a_valid_duration() -> None:
    engine = started(Clock())
    grant = claimed(engine)

    with pytest.raises(InvalidPayload, match="10x"):
        engine.report(grant.task, waiting_with_a_timer(after="10x"), supervisor="box-1")


def test_a_timer_must_have_a_valid_payload() -> None:
    engine = started(Clock())
    engine.fire("T-1", "done", None)
    grant = claimed(engine)
    timer = Schedule(event="passed", payload={"passed": "x"}, after="1m")
    outcome = Outcome(kind="waiting", schedules=(timer,))

    with pytest.raises(InvalidPayload, match="passed"):
        engine.report(grant.task, outcome, supervisor="box-1")


def test_a_retry_drops_the_timers_of_the_failed_attempt_but_keeps_the_timeout() -> None:
    clock = Clock()
    engine = started(clock)
    grant = claimed(engine)
    engine.report(grant.task, waiting_with_a_timer(), supervisor="box-1")
    engine.report(grant.task, Outcome(kind="failed", error="CI: the build broke"))

    clock.advance(minutes=10)
    assert engine.tick() == 0
    assert engine.get("T-1").state == "coding"

    clock.advance(hours=5, minutes=50)
    assert engine.tick() == 1
    assert engine.get("T-1").state == "triage"


# ── leases ───────────────────────────────────────────────────────────────


def test_a_claimed_task_whose_lease_ends_is_a_failed_attempt_and_is_queued_again() -> None:
    clock = Clock()
    engine = started(clock)
    grant = claimed(engine)

    clock.advance(minutes=2, seconds=1)

    assert engine.tick() == 1
    failed = next(e for e in engine.history("T-1") if e.kind == "task_failed")
    assert failed.data["error"] == "box-1 stopped answering: no heartbeat for 2 minutes"
    assert claimed(engine, "box-2").attempt == 2
    assert engine.heartbeat("box-1", [grant.task]) == [grant.task]


def test_a_heartbeat_keeps_the_lease() -> None:
    clock = Clock()
    engine = started(clock)
    grant = claimed(engine)

    clock.advance(minutes=1, seconds=30)
    assert engine.heartbeat("box-1", [grant.task]) == []
    clock.advance(minutes=1, seconds=30)

    assert engine.tick() == 0
    assert engine.heartbeat("box-1", [grant.task]) == []


def test_a_lost_task_with_no_retries_left_gets_error() -> None:
    clock = Clock()
    engine = started(clock)
    engine.fire("T-1", "done", None)
    claimed(engine)  # testing: retries=0, no transition on error

    clock.advance(minutes=2, seconds=1)
    engine.tick()

    assert engine.get("T-1").status == "failed"


def test_a_waiting_task_has_no_lease() -> None:
    clock = Clock()
    engine = started(clock)
    grant = claimed(engine)
    engine.report(grant.task, Outcome(kind="waiting"), supervisor="box-1")

    clock.advance(hours=1)

    assert engine.tick() == 0
    assert "task_failed" not in kinds(engine, "T-1")


def test_a_lost_supervisors_late_report_is_refused() -> None:
    clock = Clock()
    engine = started(clock)
    grant = claimed(engine)
    clock.advance(minutes=2, seconds=1)
    engine.tick()

    with pytest.raises(TaskNotHeld):
        engine.report(grant.task, Outcome(kind="event", event="done"), supervisor="box-1")

    assert engine.get("T-1").state == "coding"
