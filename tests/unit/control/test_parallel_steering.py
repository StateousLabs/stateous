"""Steering a parent: pause and resume go to its children, cancel ends them, and a
child cannot be steered on its own (design section 8).

Ported from stateous-flow `tests/unit/control/test_cancel.py` (lines 48-112),
`test_pause.py` (lines 124-136) and `test_timers.py` (lines 260-305)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from stateous import END, START, Workflow
from stateous.compiler.compile import compile_workflow
from stateous.control.engine import ChildToken, Engine, Token, child_id
from stateous.control.graph import Graph
from stateous.control.store import Store
from stateous.model.wire import Outcome, Schedule

from ...parallel import DESIGN, SECURITY, TOP, parallel_document
from .support import Clock, kinds


def at_reviews(clock: Clock | None = None) -> Engine:
    """T-1 in `reviews`; its two children in `triage`."""
    engine = Engine(Store("sqlite://"), Graph(parallel_document()), clock=clock or Clock())
    engine.start(TOP, {"repo": "acme/api"})
    engine.fire(TOP, "done", None)
    return engine


def with_a_child_timer(clock: Clock) -> Engine:
    """`at_reviews`, and the security child's routine waits, with a timer: `lgtm` in a day."""
    engine = at_reviews(clock)
    grant = engine.claim("box", ["security/triage"])
    assert grant is not None
    timer = Schedule(event="lgtm", payload=None, after="1d")
    engine.report(grant.task, Outcome(kind="waiting", schedules=(timer,)), supervisor="box")
    return engine


def nested_document() -> dict[str, Any]:
    """`top` runs `middle` in `outer`; `middle` runs `leaf` in `inner`; `leaf` ends on `ok`."""
    leaf = Workflow("leaf")
    work = leaf.state("work")
    leaf.transition(START, work)
    leaf.transition(work, END, on=leaf.event("ok"))
    middle = Workflow("middle")
    inner = middle.state("inner", parallel=[leaf])
    middle.transition(START, inner)
    middle.transition(inner, END, on="joined")
    top = Workflow("top")
    outer = top.state("outer", parallel=[middle])
    top.transition(START, outer)
    top.transition(outer, END, on="joined")
    return compile_workflow(top)


def statuses(engine: Engine, *tokens: str) -> list[str]:
    return [engine.get(token).status for token in tokens]


# ── cancel ────────────────────────────────────────────────────────────────


def test_cancelling_the_parent_cancels_its_children_and_their_tasks() -> None:
    engine = at_reviews()
    grant = engine.claim("box", ["security/triage"])
    assert grant is not None

    engine.cancel(TOP)

    assert statuses(engine, TOP, SECURITY, DESIGN) == ["cancelled"] * 3
    assert engine.heartbeat("box", [grant.task]) == [grant.task]  # the supervisor stops it
    cancelled = next(e for e in engine.history(SECURITY) if e.kind == "cancelled")
    assert cancelled.data == {"reason": "the parent was cancelled"}
    assert next(e for e in engine.history(TOP) if e.kind == "cancelled").data == {}
    accepted = [e.data["event"] for e in engine.history(TOP) if e.kind == "event_accepted"]
    assert "joined" not in accepted  # cancelled children make no join


def test_cancelling_a_paused_parent_drops_what_its_children_held() -> None:
    engine = at_reviews()
    engine.pause(TOP)
    engine.fire(SECURITY, "lgtm", None)

    engine.cancel(TOP)

    dropped = [e.data for e in engine.history(SECURITY) if e.kind == "event_dropped"]
    assert dropped == [{"event": "lgtm", "reason": "the token was cancelled"}]
    assert statuses(engine, TOP, SECURITY, DESIGN) == ["cancelled"] * 3


def test_cancelling_the_parent_cancels_its_childrens_timers() -> None:
    clock = Clock()
    engine = with_a_child_timer(clock)

    engine.cancel(TOP)
    clock.advance(hours=48)
    engine.tick()

    assert "timer_fired" not in kinds(engine, SECURITY)


# ── a child alone ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
def test_a_child_cannot_be_steered_on_its_own(action: str) -> None:
    engine = at_reviews()
    steer: Callable[[str], Token] = getattr(engine, action)

    with pytest.raises(
        ChildToken, match=f"it is part of its parent's work.*; {action} 'T-1' instead"
    ):
        steer(SECURITY)

    assert statuses(engine, TOP, SECURITY) == ["active", "active"]


def test_a_grandchild_names_the_top_token_to_steer() -> None:
    engine = Engine(Store("sqlite://"), Graph(nested_document()), clock=Clock())
    engine.start(TOP, {})
    middle = child_id(TOP, "outer", 1, "middle")
    leaf = child_id(middle, "inner", 1, "leaf")

    with pytest.raises(ChildToken, match=f"is a child of {middle!r}.*; cancel 'T-1' instead"):
        engine.cancel(leaf)


# ── pause and resume ──────────────────────────────────────────────────────


def test_pausing_the_parent_pauses_its_children_and_resume_applies_what_they_held() -> None:
    engine = at_reviews()

    engine.pause(TOP)

    assert statuses(engine, TOP, SECURITY, DESIGN) == ["paused"] * 3
    for child in (SECURITY, DESIGN):
        fired = engine.fire(child, "lgtm", None)
        assert (fired.token.state, fired.token.status) == ("triage", "paused")
        engine.fire(child, "recorded", None)
    assert engine.get(TOP).state == "reviews"

    resumed = engine.resume(TOP)

    assert (resumed.state, resumed.status) == ("merging", "active")  # the join was met
    assert statuses(engine, SECURITY, DESIGN) == ["completed", "completed"]


def test_the_children_of_a_paused_parent_hand_out_no_tasks() -> None:
    engine = at_reviews()
    engine.pause(TOP)

    assert engine.claim("box", ["security/triage", "design/triage"]) is None
    engine.resume(TOP)
    assert engine.claim("box", ["security/triage"]) is not None


def test_resuming_the_parent_gives_its_childrens_timers_the_paused_time() -> None:
    clock = Clock()
    engine = with_a_child_timer(clock)
    engine.pause(TOP)
    clock.advance(hours=5)
    engine.resume(TOP)

    clock.advance(hours=19)  # a day since the timer was set
    engine.tick()
    assert "timer_fired" not in kinds(engine, SECURITY)

    clock.advance(hours=5)
    engine.tick()
    assert "timer_fired" in kinds(engine, SECURITY)
    assert engine.get(SECURITY).state == "approved"


# ── resume applies the family's held events in the order they arrived ─────


def test_resume_applies_the_childrens_held_events_in_the_order_they_arrived() -> None:
    engine = Engine(Store("sqlite://"), Graph(parallel_document("any")), clock=Clock())
    engine.start(TOP, {"repo": "acme/api"})
    engine.fire(TOP, "done", None)
    design = engine.claim("box", ["design/triage"])
    security = engine.claim("box", ["security/triage"])
    assert design is not None and security is not None
    engine.pause(TOP)
    engine.report(design.task, Outcome(kind="event", event="lgtm"), supervisor="box")
    engine.fire(DESIGN, "recorded", None)
    engine.report(security.task, Outcome(kind="event", event="lgtm"), supervisor="box")
    engine.fire(SECURITY, "recorded", None)

    resumed = engine.resume(TOP)

    assert resumed.state == "merging"
    assert statuses(engine, DESIGN, SECURITY) == ["completed", "cancelled"]
    accepted = next(
        e for e in reversed(engine.history(TOP))
        if e.kind == "event_accepted" and e.data["event"] == "joined"
    )  # fmt: skip
    assert [b["workflow"] for b in accepted.data["payload"]["branches"]] == ["design"]
    dropped = [e.data["event"] for e in engine.history(SECURITY) if e.kind == "event_dropped"]
    assert dropped == ["lgtm", "recorded"]
    assert "event_replayed" not in kinds(engine, SECURITY)


def with_abort() -> dict[str, Any]:
    """`parallel_document()`, and an outside event `abort` that takes `reviews` back to
    `coding`."""
    document = parallel_document()
    document["events"]["abort"] = {}
    document["transitions"].append({"from": "reviews", "to": "coding", "on": "abort"})
    return document


def test_a_parents_event_held_before_a_childs_outcome_applies_first() -> None:
    engine = Engine(Store("sqlite://"), Graph(with_abort()), clock=Clock())
    engine.start(TOP, {"repo": "acme/api"})
    engine.fire(TOP, "done", None)
    engine.pause(TOP)
    engine.fire(TOP, "abort", None)
    engine.fire(SECURITY, "lgtm", None)
    engine.fire(SECURITY, "recorded", None)

    resumed = engine.resume(TOP)

    assert resumed.state == "coding"
    assert statuses(engine, SECURITY, DESIGN) == ["cancelled", "cancelled"]
    dropped = [e.data["event"] for e in engine.history(SECURITY) if e.kind == "event_dropped"]
    assert dropped == ["lgtm", "recorded"]


# ── a grandchild: pause, resume and cancel reach it through each level ────


def nested_engine() -> tuple[Engine, str, str]:
    """`nested_document()` with T-1 started: the engine, the middle child, the leaf."""
    engine = Engine(Store("sqlite://"), Graph(nested_document()), clock=Clock())
    engine.start(TOP, {})
    middle = child_id(TOP, "outer", 1, "middle")
    return engine, middle, child_id(middle, "inner", 1, "leaf")


def test_pause_and_resume_reach_a_grandchild_and_its_held_event_joins_each_level() -> None:
    engine, middle, leaf = nested_engine()

    engine.pause(TOP)

    assert statuses(engine, TOP, middle, leaf) == ["paused"] * 3
    assert engine.claim("box", ["leaf/work"]) is None
    engine.fire(leaf, "ok", None)
    assert statuses(engine, TOP, middle, leaf) == ["paused"] * 3

    resumed = engine.resume(TOP)

    assert resumed.status == "completed"
    assert statuses(engine, middle, leaf) == ["completed", "completed"]
    assert "event_replayed" in kinds(engine, leaf)


def test_cancelling_the_top_token_cancels_a_paused_grandchild_and_drops_what_it_held() -> None:
    engine, middle, leaf = nested_engine()
    engine.pause(TOP)
    engine.fire(leaf, "ok", None)

    engine.cancel(TOP)

    assert statuses(engine, TOP, middle, leaf) == ["cancelled"] * 3
    for child in (middle, leaf):
        cancelled = next(e for e in engine.history(child) if e.kind == "cancelled")
        assert cancelled.data == {"reason": "the parent was cancelled"}
    dropped = [e.data for e in engine.history(leaf) if e.kind == "event_dropped"]
    assert dropped == [{"event": "ok", "reason": "the token was cancelled"}]
