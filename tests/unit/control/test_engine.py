"""The engine: starting tokens and routing events, one transaction each."""

import pytest

from stateous.control.engine import (
    EventNotAllowed,
    InvalidContext,
    InvalidPayload,
    InvalidTokenId,
    RecordedGraph,
    TokenNotActive,
    TokenNotFound,
)

from .support import TESTS_FAILED, TESTS_PASSED, TICKET, Clock, kinds, new_engine, sdlc

# ── starting ──────────────────────────────────────────────────────────────


def test_a_token_starts_where_the_otherwise_transition_from_start_leads() -> None:
    engine = new_engine()

    token = engine.start("T-1", TICKET)

    assert (token.token, token.state, token.status, token.visit) == ("T-1", "coding", "active", 1)
    assert token.context == {"repo": "acme/api", "rejected": 0, "urgent": False}


def test_the_guards_of_start_choose_on_the_starting_context() -> None:
    assert new_engine().start("T-1", {"repo": "acme/api", "urgent": True}).state == "triage"


def test_the_history_shows_the_token_leaving_start() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)

    history = engine.history("T-1")

    assert [e.kind for e in history] == [
        "started", "event_accepted", "entered", "task_queued", "timer_set",
    ]  # fmt: skip
    assert history[0].data == {"context": {"repo": "acme/api", "rejected": 0, "urgent": False}}
    assert history[1].data == {"event": "__started__", "payload": None, "state": "__start__"}
    assert history[2].data == {"state": "coding", "visit": 1}
    assert history[4].data == {"event": "timeout", "due_at": "2026-10-06T15:00:00Z"}


def test_starting_an_existing_token_returns_it_unchanged() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)

    again = engine.start("T-1", {"repo": "other"})

    assert again.context["repo"] == "acme/api"
    assert kinds(engine, "T-1").count("started") == 1


def test_a_context_that_breaks_its_schema_starts_nothing() -> None:
    engine = new_engine()

    with pytest.raises(InvalidContext, match="repo"):
        engine.start("T-1", {})

    with pytest.raises(TokenNotFound):
        engine.get("T-1")


def test_a_start_that_no_transition_holds_for_is_refused_and_records_nothing() -> None:
    document = sdlc()
    document["transitions"] = [
        t for t in document["transitions"] if not (t["from"] == "__start__" and "otherwise" in t)
    ]
    engine = new_engine(document)

    with pytest.raises(InvalidContext, match="no transition from START holds"):
        engine.start("T-1", TICKET)

    assert engine.tokens() == []


def test_a_token_id_differing_only_in_case_from_an_existing_one_is_refused() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)

    with pytest.raises(InvalidTokenId, match="compared without regard to case"):
        engine.start("t-1", TICKET)

    assert [t.token for t in engine.tokens()] == ["T-1"]


@pytest.mark.parametrize("token", ["", "a b", "a/b", ".ab", "x" * 129])
def test_a_token_id_outside_the_pattern_is_refused(token: str) -> None:
    with pytest.raises(InvalidTokenId, match="letter or digit"):
        new_engine().start(token, TICKET)


def test_a_token_id_of_128_characters_is_accepted() -> None:
    token = "x" * 128

    started = new_engine().start(token, TICKET)

    assert started.token == token


# ── moving ────────────────────────────────────────────────────────────────


def test_an_event_moves_the_token_and_queues_the_next_task() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)

    fired = engine.fire("T-1", "done", None)

    assert (fired.token.state, fired.token.visit, fired.duplicate) == ("testing", 1, False)
    assert kinds(engine, "T-1")[-5:] == [
        "event_accepted", "task_cancelled", "entered", "task_queued", "timer_set",
    ]  # fmt: skip


def test_an_accepted_event_is_recorded_with_its_payload_and_its_state() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    engine.fire("T-1", "done", None)

    engine.fire("T-1", "passed", TESTS_PASSED)

    accepted = [e for e in engine.history("T-1") if e.kind == "event_accepted"][-1]
    assert accepted.data == {"event": "passed", "payload": TESTS_PASSED, "state": "testing"}


def test_reaching_end_completes_the_token_in_the_state_it_came_from() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    engine.fire("T-1", "done", None)

    token = engine.fire("T-1", "passed", TESTS_PASSED).token

    assert (token.state, token.status) == ("testing", "completed")
    assert kinds(engine, "T-1")[-1] == "completed"


def test_count_includes_the_event_being_routed() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    for _ in range(2):
        engine.fire("T-1", "done", None)
        assert engine.fire("T-1", "failed", TESTS_FAILED).token.state == "coding"
    engine.fire("T-1", "done", None)

    third = engine.fire("T-1", "failed", TESTS_FAILED).token

    assert (third.state, third.status) == ("testing", "completed")


def test_the_visit_counts_entries_into_a_state() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    engine.fire("T-1", "done", None)
    engine.fire("T-1", "failed", TESTS_FAILED)

    token = engine.fire("T-1", "done", None).token

    assert (token.state, token.visit) == ("testing", 2)


def test_two_guards_that_both_hold_fail_the_token_and_say_why() -> None:
    document = sdlc()
    for transition in document["transitions"]:
        if transition["from"] == "testing" and transition.get("otherwise"):
            del transition["otherwise"]
            transition["when"] = "count('failed') >= 1"
    engine = new_engine(document)
    engine.start("T-1", TICKET)
    engine.fire("T-1", "done", None)

    token = engine.fire("T-1", "failed", TESTS_FAILED).token

    assert (token.state, token.status) == ("testing", "failed")
    failed = engine.history("T-1")[-1]
    assert failed.kind == "failed"
    assert "more than one guard" in failed.data["reason"]


# ── what is refused, and how ─────────────────────────────────────────────


def test_an_unknown_token_is_an_error() -> None:
    with pytest.raises(TokenNotFound, match="T-404"):
        new_engine().fire("T-404", "done", None)


def test_an_event_the_workflow_does_not_declare_is_refused_and_recorded() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)

    with pytest.raises(EventNotAllowed, match="no event 'dnoe'"):
        engine.fire("T-1", "dnoe", None)

    last = engine.history("T-1")[-1]
    assert (last.kind, last.data["event"]) == ("event_rejected", "dnoe")


@pytest.mark.parametrize("event", ["timeout", "error", "__started__"])
def test_no_one_outside_may_fire_stateouss_own_events(event: str) -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)

    with pytest.raises(EventNotAllowed, match="Stateous's own event"):
        engine.fire("T-1", event, None)

    assert engine.get("T-1").state == "coding"


def test_an_event_with_no_transition_from_the_state_is_refused_and_recorded() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)

    with pytest.raises(EventNotAllowed, match="no transition for 'passed' from 'coding'"):
        engine.fire("T-1", "passed", TESTS_PASSED)

    assert engine.history("T-1")[-1].kind == "event_rejected"
    assert engine.get("T-1").state == "coding"


def test_an_invalid_payload_is_refused_and_records_nothing() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    engine.fire("T-1", "done", None)
    before = len(engine.history("T-1"))

    with pytest.raises(InvalidPayload, match="passed"):
        engine.fire("T-1", "passed", {"passed": "many", "failed": 0})

    assert len(engine.history("T-1")) == before


def test_an_event_for_a_completed_token_is_refused() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    engine.fire("T-1", "stuck", None)

    with pytest.raises(TokenNotActive, match="completed"):
        engine.fire("T-1", "done", None)


# ── idempotency keys ─────────────────────────────────────────────────────


def test_a_repeated_idempotency_key_is_applied_once() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)

    first = engine.fire("T-1", "done", None, key="delivery-7")
    again = engine.fire("T-1", "done", None, key="delivery-7")

    assert (first.duplicate, again.duplicate) == (False, True)
    assert again.token.state == "testing"
    assert kinds(engine, "T-1").count("event_accepted") == 2  # __started__ and done


def test_a_refused_event_does_not_use_up_its_key() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    with pytest.raises(EventNotAllowed):
        engine.fire("T-1", "passed", TESTS_PASSED, key="k")
    engine.fire("T-1", "done", None)

    assert not engine.fire("T-1", "passed", TESTS_PASSED, key="k").duplicate


@pytest.mark.parametrize("key", ["", "k" * 257])
def test_an_idempotency_key_must_be_1_to_256_characters(key: str) -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)
    before = len(engine.history("T-1"))

    with pytest.raises(InvalidPayload, match="1 to 256"):
        engine.fire("T-1", "done", None, key=key)

    assert len(engine.history("T-1")) == before


# ── reading ──────────────────────────────────────────────────────────────


def test_tokens_are_listed_newest_first_and_by_status() -> None:
    clock = Clock()
    engine = new_engine(clock=clock)
    for token in ("T-1", "T-2", "T-3"):
        engine.start(token, TICKET)
        clock.advance(minutes=1)
    engine.fire("T-1", "stuck", None)

    assert [t.token for t in engine.tokens()] == ["T-3", "T-2", "T-1"]
    assert [t.token for t in engine.tokens(status="completed")] == ["T-1"]
    assert [t.token for t in engine.tokens(limit=1)] == ["T-3"]


def test_times_are_in_utc() -> None:
    clock = Clock()
    engine = new_engine(clock=clock)

    token = engine.start("T-1", TICKET)

    assert token.entered_at == clock.now
    assert engine.history("T-1")[0].at == clock.now


def test_the_history_of_an_unknown_token_is_an_error() -> None:
    with pytest.raises(TokenNotFound):
        new_engine().history("T-404")


# ── the graph on record, for serve ───────────────────────────────────────


def test_a_new_database_has_no_graph_on_record() -> None:
    assert new_engine().recorded_graph() is None


def test_the_graph_on_record_is_the_one_recorded_last() -> None:
    engine = new_engine()

    engine.record_graph()

    assert engine.recorded_graph() == RecordedGraph(engine.graph.structure, engine.graph.checksum)


def test_live_tokens_counts_only_the_tokens_that_can_still_move() -> None:
    engine = new_engine()
    for token in ("T-1", "T-2", "T-3"):
        engine.start(token, TICKET)
    engine.fire("T-3", "stuck", None)

    assert engine.live_tokens() == 2
