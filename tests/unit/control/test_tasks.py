"""Tasks: a supervisor claims and reports; retries, then `error`; notes; memory."""

from datetime import UTC, datetime

import pytest

from stateous.control.engine import (
    AttemptOver,
    ChildToken,
    Engine,
    EngineError,
    EventNotAllowed,
    InvalidArgument,
    InvalidContext,
    InvalidPayload,
    InvalidTokenId,
    TaskNotFound,
    TaskNotHeld,
    TokenNotActive,
    TokenNotFound,
)
from stateous.model.wire import EventRecord, Outcome, TaskGrant

from .support import TESTS_FAILED, TESTS_PASSED, TICKET, Clock, kinds, new_engine

STATES = ["triage", "coding", "testing"]


def claimed(engine: Engine, supervisor: str = "box-1") -> TaskGrant:
    grant = engine.claim(supervisor, STATES)
    assert grant is not None
    return grant


def done() -> Outcome:
    return Outcome(kind="event", event="done")


def failed(error: str = "RuntimeError: boom") -> Outcome:
    return Outcome(kind="failed", error=error)


def memory_of(engine: Engine, token: str, state: str) -> dict[str, object]:
    with engine.store.transaction(datetime.now(UTC)) as tx:
        return tx.memory(token, state)


# ── claiming ─────────────────────────────────────────────────────────────


def test_a_claim_hands_out_the_task_with_what_the_routine_needs() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)

    grant = engine.claim("box-1", ["coding"])

    assert grant is not None
    assert (grant.token, grant.state, grant.visit, grant.attempt) == ("T-1", "coding", 1, 1)
    assert grant.context == {"repo": "acme/api", "rejected": 0, "urgent": False}
    assert grant.memory == {}
    assert grant.entered_by == EventRecord("__started__", None)
    claim = engine.history("T-1")[-1]
    assert (claim.kind, claim.data) == (
        "task_claimed", {"task": grant.task, "supervisor": "box-1", "attempt": 1},
    )  # fmt: skip


def test_a_claim_takes_only_the_states_the_supervisor_serves() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)

    assert engine.claim("box-1", ["testing"]) is None
    assert engine.claim("box-1", []) is None


def test_a_task_is_claimed_once() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    claimed(engine)

    assert engine.claim("box-2", STATES) is None


def test_the_next_task_is_told_which_event_brought_the_token() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    engine.fire("T-1", "done", None)
    testing = claimed(engine)
    assert testing.entered_by == EventRecord("done", None)

    outcome = Outcome(kind="event", event="failed", payload=TESTS_FAILED)
    engine.report(testing.task, outcome, supervisor="box-1")

    coding = claimed(engine)
    assert (coding.state, coding.visit) == ("coding", 2)
    assert coding.entered_by == EventRecord("failed", TESTS_FAILED)


# ── reporting ────────────────────────────────────────────────────────────


def test_a_reported_event_moves_the_token_and_the_task_is_done() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)

    fired = engine.report(grant.task, done(), supervisor="box-1")

    assert fired.token.state == "testing"
    assert "task_done" in kinds(engine, "T-1")
    assert engine.heartbeat("box-1", [grant.task]) == [grant.task]


def test_a_reports_own_routing_does_not_use_up_a_key_a_caller_might_fire_with() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)

    engine.report(grant.task, done(), supervisor="box-1")

    # The task's own report moved the token with no idempotency key of its own.
    # A caller is free to use a key that happens to look like the task's id.
    fired = engine.fire("T-1", "passed", TESTS_PASSED, key=f"task:{grant.task}")

    assert not fired.duplicate
    assert fired.token.status == "completed"


def test_the_routines_context_changes_are_saved_with_the_outcome() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)

    outcome = Outcome(kind="event", event="done", context={"rejected": 2})
    engine.report(grant.task, outcome, supervisor="box-1")

    assert engine.get("T-1").context["rejected"] == 2
    changed = [e for e in engine.history("T-1") if e.kind == "context_changed"]
    assert [e.data for e in changed] == [{"keys": ["rejected"]}]


def test_context_changes_that_break_the_schema_are_refused_and_nothing_is_saved() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)

    outcome = Outcome(kind="event", event="done", context={"rejected": "x"})

    with pytest.raises(InvalidContext, match="rejected"):
        engine.report(grant.task, outcome, supervisor="box-1")

    token = engine.get("T-1")
    assert (token.state, token.context["rejected"]) == ("coding", 0)
    assert engine.heartbeat("box-1", [grant.task]) == []  # still held


def test_an_event_the_state_may_not_emit_is_refused_and_recorded() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)

    outcome = Outcome(kind="event", event="passed", payload=TESTS_FAILED)

    with pytest.raises(EventNotAllowed, match="may not emit 'passed'"):
        engine.report(grant.task, outcome, supervisor="box-1")

    assert engine.history("T-1")[-1].kind == "event_rejected"
    assert engine.heartbeat("box-1", [grant.task]) == []


def test_an_invalid_payload_in_a_report_is_refused() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    engine.fire("T-1", "done", None)
    grant = claimed(engine)

    outcome = Outcome(kind="event", event="passed", payload={"passed": "x"})

    with pytest.raises(InvalidPayload, match="passed"):
        engine.report(grant.task, outcome, supervisor="box-1")


def test_a_waiting_task_ends_its_lease_and_a_report_from_outside_moves_the_token() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)

    waiting = engine.report(grant.task, Outcome(kind="waiting"), supervisor="box-1")

    assert waiting.token.state == "coding"
    assert kinds(engine, "T-1")[-1] == "task_waiting"
    assert engine.heartbeat("box-1", [grant.task]) == [grant.task]  # no longer its own
    assert engine.report(grant.task, done()).token.state == "testing"


def test_a_waiting_task_is_done_when_a_fired_event_moves_the_token() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)
    engine.report(grant.task, Outcome(kind="waiting"), supervisor="box-1")

    engine.fire("T-1", "done", None)

    assert kinds(engine, "T-1")[-5:-3] == ["event_accepted", "task_done"]


def test_only_the_supervisor_that_holds_a_task_reports_it() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)

    with pytest.raises(TaskNotHeld, match="box-2"):
        engine.report(grant.task, done(), supervisor="box-2")
    with pytest.raises(TaskNotHeld, match="waiting"):
        engine.report(grant.task, Outcome(kind="waiting"))
    with pytest.raises(TaskNotFound):
        engine.report(999, done())


def test_a_task_that_is_done_takes_no_more_reports() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)
    engine.report(grant.task, done(), supervisor="box-1")

    with pytest.raises(TaskNotHeld, match="done"):
        engine.report(grant.task, done(), supervisor="box-1")


# ── retries ──────────────────────────────────────────────────────────────


def test_a_failed_attempt_is_tried_again_while_retries_allow() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)

    fired = engine.report(grant.task, failed(), supervisor="box-1")

    assert fired.token.state == "coding"
    history = engine.history("T-1")
    assert [e.kind for e in history[-2:]] == ["task_failed", "task_retrying"]
    assert history[-2].data == {"task": grant.task, "error": "RuntimeError: boom"}
    assert history[-1].data == {"task": grant.task, "attempt": 2}
    again = claimed(engine, "box-2")
    assert (again.task, again.attempt) == (grant.task, 2)


def test_when_the_retries_are_used_up_the_token_gets_error() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    for _ in range(3):  # coding: retries=2, so three attempts
        engine.report(claimed(engine).task, failed(), supervisor="box-1")

    assert engine.get("T-1").state == "triage"  # coding --error--> triage
    accepted = [e for e in engine.history("T-1") if e.kind == "event_accepted"][-1]
    assert accepted.data["event"] == "error"


def test_error_with_no_transition_fails_the_token_and_says_why() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    engine.fire("T-1", "done", None)
    grant = claimed(engine)  # testing: retries=0, no transition on error

    engine.report(grant.task, failed("AssertionError: 2 tests failed"), supervisor="box-1")

    token = engine.get("T-1")
    assert (token.state, token.status) == ("testing", "failed")
    last = engine.history("T-1")[-1]
    assert last.kind == "failed"
    assert "AssertionError: 2 tests failed" in last.data["reason"]


def test_a_failed_report_from_outside_is_an_attempt_too() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)
    engine.report(grant.task, Outcome(kind="waiting"), supervisor="box-1")

    engine.report(grant.task, failed("CI: the build broke"))

    assert claimed(engine).attempt == 2


# ── heartbeats ───────────────────────────────────────────────────────────


def test_a_heartbeat_names_the_tasks_the_supervisor_no_longer_holds() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    engine.start("T-2", TICKET)
    first, second = claimed(engine), claimed(engine)
    assert second.token == "T-2"
    engine.fire("T-2", "stuck", None)  # T-2 ends: its task is done

    assert engine.heartbeat("box-1", [first.task, second.task, 999]) == [second.task, 999]
    assert engine.heartbeat("box-2", [first.task]) == [first.task]


def test_a_heartbeat_does_not_revive_a_task_whose_lease_already_ended() -> None:
    clock = Clock()
    engine = new_engine(clock=clock)
    engine.start("T-1", TICKET)
    grant = claimed(engine)
    clock.advance(minutes=3)  # past the default 2-minute lease

    assert engine.heartbeat("box-1", [grant.task]) == [grant.task]  # lost, not renewed


# ── notes ────────────────────────────────────────────────────────────────


def test_a_note_is_a_line_of_progress_in_the_history() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)

    engine.note(grant.task, "  wrote the tests  ")

    last = engine.history("T-1")[-1]
    assert (last.kind, last.data) == ("note", {"task": grant.task, "text": "wrote the tests"})


@pytest.mark.parametrize("text", ["", "   ", "x" * 501])
def test_a_note_is_1_to_500_characters(text: str) -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)

    with pytest.raises(InvalidPayload, match="1 to 500"):
        engine.note(grant.task, text)


def test_a_note_for_a_task_no_one_works_on_is_refused() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)
    engine.report(grant.task, done(), supervisor="box-1")

    with pytest.raises(TaskNotHeld):
        engine.note(grant.task, "late")


# ── the routine memory ───────────────────────────────────────────────────


def test_memory_is_saved_at_once_and_the_history_records_the_key_only() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)

    engine.set_memory(grant.task, "dev", "session-1")

    last = engine.history("T-1")[-1]
    assert (last.kind, last.data) == ("memory_set", {"state": "coding", "memory_key": "dev"})
    assert "session-1" not in repr([e.data for e in engine.history("T-1")])
    assert memory_of(engine, "T-1", "coding") == {"dev": "session-1"}


def test_a_retry_and_a_later_visit_see_the_memory_of_their_state() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    first = claimed(engine)
    engine.set_memory(first.task, "dev", "s-1")
    engine.report(first.task, failed(), supervisor="box-1")

    retry = claimed(engine)
    assert retry.memory == {"dev": "s-1"}
    engine.report(retry.task, done(), supervisor="box-1")
    testing = claimed(engine)
    assert testing.memory == {}
    outcome = Outcome(kind="event", event="failed", payload=TESTS_FAILED)
    engine.report(testing.task, outcome, supervisor="box-1")

    back = claimed(engine)
    assert (back.state, back.visit, back.memory) == ("coding", 2, {"dev": "s-1"})


def test_none_deletes_a_key_and_deleting_an_absent_key_records_nothing() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)
    engine.set_memory(grant.task, "dev", "s-1")

    engine.set_memory(grant.task, "dev", None)
    engine.set_memory(grant.task, "dev", None)

    assert kinds(engine, "T-1")[-2:] == ["memory_set", "memory_deleted"]
    assert memory_of(engine, "T-1", "coding") == {}


def test_memory_is_deleted_when_the_token_ends() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)
    engine.set_memory(grant.task, "dev", "s-1")

    engine.report(grant.task, Outcome(kind="event", event="stuck"), supervisor="box-1")

    assert engine.get("T-1").status == "completed"
    assert memory_of(engine, "T-1", "coding") == {}


@pytest.mark.parametrize("value", [float("nan"), {1, 2}])
def test_a_memory_value_that_is_not_json_is_refused(value: object) -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)

    with pytest.raises(InvalidPayload, match="not JSON"):
        engine.set_memory(grant.task, "dev", value)

    assert memory_of(engine, "T-1", "coding") == {}


@pytest.mark.parametrize("key", ["", "k" * 129])
def test_a_memory_key_is_1_to_128_characters(key: str) -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)

    with pytest.raises(InvalidPayload, match="1 to 128"):
        engine.set_memory(grant.task, key, "v")


def test_a_report_that_routing_refuses_after_the_decision_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)

    def refuses(*_: object, **__: object) -> str:
        return "a refusal the decision did not foresee"

    monkeypatch.setattr(engine, "_route", refuses)

    with pytest.raises(RuntimeError, match="a refusal the decision did not foresee"):
        engine.report(grant.task, done(), supervisor="box-1")


# ── attempts ─────────────────────────────────────────────────────────────


def requeued(engine: Engine) -> TaskGrant:
    """Attempt 1 of coding fails; attempt 2 is claimed. Returns the new grant."""
    engine.start("T-1", TICKET)
    first = claimed(engine)
    engine.report(first.task, failed(), supervisor="box-1", attempt=1)
    again = claimed(engine)
    assert (again.task, again.attempt) == (first.task, 2)
    return again


def test_a_late_report_from_an_old_attempt_is_refused_and_does_not_move_the_token() -> None:
    engine = new_engine()
    grant = requeued(engine)

    with pytest.raises(TaskNotHeld, match=r"attempt 2.*attempt 1"):
        engine.report(grant.task, done(), supervisor="box-1", attempt=1)
    with pytest.raises(TaskNotHeld, match=r"attempt 2.*attempt 1"):
        engine.report(grant.task, Outcome(kind="event", event="done", attempt=1))

    assert engine.get("T-1").state == "coding"
    assert engine.report(grant.task, done(), supervisor="box-1", attempt=2).token.state == "testing"


def test_the_attempt_in_the_outcome_is_checked_too() -> None:
    engine = new_engine()
    grant = requeued(engine)

    outcome = Outcome(kind="event", event="done", attempt=2)

    assert engine.report(grant.task, outcome, supervisor="box-1").token.state == "testing"


def test_a_note_or_memory_from_an_old_attempt_is_refused() -> None:
    engine = new_engine()
    grant = requeued(engine)

    with pytest.raises(TaskNotHeld, match=r"attempt 2.*attempt 1"):
        engine.note(grant.task, "late", attempt=1)
    with pytest.raises(TaskNotHeld, match=r"attempt 2.*attempt 1"):
        engine.set_memory(grant.task, "dev", "s-1", attempt=1)

    engine.note(grant.task, "now", attempt=2)
    engine.set_memory(grant.task, "dev", "s-2", attempt=2)
    assert memory_of(engine, "T-1", "coding") == {"dev": "s-2"}


def test_a_request_for_an_old_attempt_raises_attempt_over() -> None:
    """The request is for an attempt that is over: `AttemptOver`, a `TaskNotHeld`,
    so the code `except TaskNotHeld` still catches it."""
    engine = new_engine()
    grant = requeued(engine)

    with pytest.raises(AttemptOver) as report:
        engine.report(grant.task, done(), supervisor="box-1", attempt=1)
    with pytest.raises(AttemptOver):
        engine.note(grant.task, "late", attempt=1)
    with pytest.raises(AttemptOver):
        engine.set_memory(grant.task, "dev", "s-1", attempt=1)

    assert isinstance(report.value, TaskNotHeld)
    assert report.value.code == "attempt_over"


def test_a_request_for_an_attempt_that_did_not_start_is_not_attempt_over() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)

    with pytest.raises(TaskNotHeld) as caught:
        engine.report(grant.task, done(), supervisor="box-1", attempt=2)

    assert not isinstance(caught.value, AttemptOver)
    assert caught.value.code == "task_not_held"


def test_a_request_for_a_task_that_is_not_held_is_task_not_held() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)

    with pytest.raises(TaskNotHeld) as other:
        engine.report(grant.task, done(), supervisor="box-2")
    engine.report(grant.task, done(), supervisor="box-1")
    with pytest.raises(TaskNotHeld) as ended:
        engine.note(grant.task, "late")

    assert (other.value.code, ended.value.code) == ("task_not_held", "task_not_held")


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (TokenNotFound, "token_not_found"),
        (TaskNotFound, "task_not_found"),
        (InvalidArgument, "invalid_argument"),
        (InvalidContext, "invalid_context"),
        (InvalidPayload, "invalid_payload"),
        (InvalidTokenId, "invalid_token_id"),
        (EventNotAllowed, "event_not_allowed"),
        (TokenNotActive, "token_not_active"),
        (ChildToken, "child_token"),
        (TaskNotHeld, "task_not_held"),
        (AttemptOver, "attempt_over"),
    ],
)
def test_each_engine_error_declares_its_code(error: type[EngineError], code: str) -> None:
    assert error.code == code
    assert error("x").code == code


def test_a_report_without_an_attempt_is_taken_as_before() -> None:
    engine = new_engine()
    grant = requeued(engine)

    engine.note(grant.task, "from outside")

    assert engine.report(grant.task, done()).token.state == "testing"


def test_the_attempt_mismatch_message_is_neutral_about_direction() -> None:
    """The request's attempt can be behind the task's, or ahead of it: the wording
    must be true both ways, not "which is over" (true only when behind)."""
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)

    with pytest.raises(TaskNotHeld, match=r"task \d+ is at attempt 1, not attempt 2"):
        engine.report(grant.task, done(), supervisor="box-1", attempt=2)


def test_a_report_refuses_when_the_attempt_argument_and_the_outcomes_attempt_differ() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)
    outcome = Outcome(kind="event", event="done", attempt=2)

    with pytest.raises(InvalidArgument, match="attempt"):
        engine.report(grant.task, outcome, supervisor="box-1", attempt=1)

    assert engine.get("T-1").state == "coding"


def test_a_report_whose_attempt_argument_matches_the_outcomes_attempt_is_accepted() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    grant = claimed(engine)
    outcome = Outcome(kind="event", event="done", attempt=1)

    assert engine.report(grant.task, outcome, supervisor="box-1", attempt=1).token.state == (
        "testing"
    )


def test_a_late_report_note_and_memory_after_the_lease_ended_are_refused_by_attempt() -> None:
    """A lease that ends counts as a failed attempt and queues the task again (see
    `_end_leases`). Attempt 1's late report, note and memory write are refused;
    attempt 2's report is accepted."""
    clock = Clock()
    engine = new_engine(clock=clock)
    engine.start("T-1", TICKET)
    grant = claimed(engine)
    clock.advance(minutes=3)  # past the default 2-minute lease
    engine.tick()
    again = claimed(engine)
    assert (again.task, again.attempt) == (grant.task, 2)

    with pytest.raises(TaskNotHeld, match=r"attempt 2.*attempt 1"):
        engine.report(grant.task, done(), supervisor="box-1", attempt=1)
    with pytest.raises(TaskNotHeld, match=r"attempt 2.*attempt 1"):
        engine.note(grant.task, "late", attempt=1)
    with pytest.raises(TaskNotHeld, match=r"attempt 2.*attempt 1"):
        engine.set_memory(grant.task, "dev", "s-1", attempt=1)

    assert engine.get("T-1").state == "coding"
    assert engine.report(again.task, done(), supervisor="box-1", attempt=2).token.state == (
        "testing"
    )
