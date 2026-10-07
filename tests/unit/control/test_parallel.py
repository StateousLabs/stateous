"""Parallel states in the engine: children, the join, and the refusals of an entry.

Ported from stateous-flow `tests/unit/control/test_parallel.py` and the parts on
branches of `tests/unit/control/test_start_end.py`, fitted to the small engine: the
child id `<parent>.<state>.<visit>.<workflow>`, the event `joined`, and a `status`
for each child."""

from __future__ import annotations

from typing import Any

import pytest

from stateous import END, START, Workflow
from stateous.compiler.compile import compile_workflow
from stateous.control.engine import Engine, EventNotAllowed, InvalidContext, child_id
from stateous.control.graph import Graph
from stateous.control.store import Store
from stateous.model.wire import Outcome

from ...parallel import DESIGN, SECURITY, TOP, parallel_document
from .support import Clock


def new(document: dict[str, Any], clock: Clock | None = None) -> Engine:
    return Engine(Store("sqlite://"), Graph(document), clock=clock or Clock())


def at_reviews(join: str = "all", clock: Clock | None = None) -> Engine:
    """T-1 in `reviews`; its two children in `triage`."""
    engine = new(parallel_document(join), clock)
    engine.start(TOP, {"repo": "acme/api"})
    engine.fire(TOP, "done", None)
    return engine


def finish(
    engine: Engine, child: str, verdict: str = "lgtm", findings: dict[str, Any] | None = None
) -> None:
    """A review reaches its verdict and, as it records it, its END."""
    engine.fire(child, verdict, findings)
    engine.fire(child, "recorded", None)


def joined(engine: Engine, token: str = TOP) -> list[dict[str, Any]]:
    """The `branches` of the last `joined` that the token accepted, by workflow."""
    entry = next(
        e for e in reversed(engine.history(token))
        if e.kind == "event_accepted" and e.data["event"] == "joined"
    )  # fmt: skip
    branches: list[dict[str, Any]] = entry.data["payload"]["branches"]

    def workflow(branch: dict[str, Any]) -> str:
        return str(branch["workflow"])

    return sorted(branches, key=workflow)


def without_context() -> dict[str, Any]:
    """`parallel_document()` without a model for the parent's context. Compile cannot
    check what the children get then, so the engine checks it."""
    document = parallel_document()
    del document["context"]
    return document


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


# ── entering ──────────────────────────────────────────────────────────────


def test_entering_a_parallel_state_starts_one_child_token_per_workflow() -> None:
    engine = at_reviews()

    security, design = engine.get(SECURITY), engine.get(DESIGN)

    assert child_id(TOP, "reviews", 1, "security") == SECURITY
    assert child_id(TOP, "reviews", 1, "design") == DESIGN
    assert (security.parent, design.parent) == (TOP, TOP)
    assert (security.state, security.status) == ("triage", "active")
    assert security.context == {"repo": "acme/api", "branch": "main"}
    assert engine.get(TOP).state == "reviews"
    started = [e.data for e in engine.history(TOP) if e.kind == "branch_started"]
    assert started == [
        {"workflow": "security", "token": SECURITY},
        {"workflow": "design", "token": DESIGN},
    ]
    first = engine.history(SECURITY)[0]
    assert (first.kind, first.data["parent"]) == ("started", TOP)
    assert engine.claim("box", ["reviews"]) is None  # a parallel state has no task
    assert sorted(t.token for t in engine.tokens(parent=TOP)) == [DESIGN, SECURITY]


def test_a_childs_task_is_claimed_by_its_workflow_and_state() -> None:
    engine = at_reviews()

    assert engine.claim("box", ["triage"]) is None  # the main workflow has no `triage`
    grant = engine.claim("box", ["security/triage"])
    assert grant is not None
    assert (grant.token, grant.state, grant.context["branch"]) == (
        SECURITY, "security/triage", "main",
    )  # fmt: skip
    fired = engine.report(grant.task, Outcome(kind="event", event="lgtm"), supervisor="box")

    assert fired.token.state == "approved"


def test_a_child_routes_with_its_own_workflow() -> None:
    engine = at_reviews()

    with pytest.raises(EventNotAllowed, match="the workflow 'security' has no event 'done'"):
        engine.fire(SECURITY, "done", None)


def test_joined_is_stateous_own_event() -> None:
    engine = at_reviews()

    with pytest.raises(EventNotAllowed, match="'joined' is Stateous's own event"):
        engine.fire(TOP, "joined", {"branches": []})


# ── the join ──────────────────────────────────────────────────────────────


def test_the_join_fires_when_every_child_has_ended() -> None:
    engine = at_reviews()

    engine.fire(SECURITY, "lgtm", None)
    assert engine.get(TOP).state == "reviews"  # security has its verdict, not its END
    engine.fire(SECURITY, "recorded", None)
    assert engine.get(TOP).state == "reviews"  # design has not ended
    finish(engine, DESIGN)

    assert engine.get(TOP).state == "merging"


def test_each_ended_child_is_one_entry_of_the_joined_payload() -> None:
    engine = at_reviews()

    finish(engine, SECURITY)
    finish(engine, DESIGN, "deny", {"count": 2})

    assert engine.get(TOP).state == "coding"  # not both approved: the fallback
    # `final` and `payload` tell of one moment: the event that brought the child into
    # its last state, not the one that took it on to END.
    assert joined(engine) == [
        {"workflow": "design", "token": DESIGN, "status": "completed", "final": "rejected",
         "payload": {"count": 2}},
        {"workflow": "security", "token": SECURITY, "status": "completed", "final": "approved",
         "payload": None},
    ]  # fmt: skip


def test_a_failed_child_is_in_the_join_with_the_state_it_failed_in() -> None:
    engine = at_reviews()
    finish(engine, SECURITY)
    engine.fire(DESIGN, "deny", {"count": 1})
    grant = engine.claim("box", ["design/rejected"])
    assert grant is not None

    engine.report(grant.task, Outcome(kind="failed", error="GitHub is down"), supervisor="box")

    design = joined(engine)[0]
    assert (design["status"], design["final"], design["payload"]) == (
        "failed", "rejected", {"count": 1},
    )  # fmt: skip
    assert engine.get(DESIGN).status == "failed"
    assert engine.get(TOP).state == "coding"


def test_coming_back_to_a_parallel_state_starts_new_children() -> None:
    engine = at_reviews()
    finish(engine, SECURITY)
    finish(engine, DESIGN, "deny", {"count": 1})

    engine.fire(TOP, "done", None)

    again = child_id(TOP, "reviews", 2, "design")
    assert engine.get(again).state == "triage"
    assert engine.get(DESIGN).status == "completed"
    assert len(engine.tokens(parent=TOP)) == 4


def test_join_any_goes_on_with_the_first_child_and_cancels_the_others() -> None:
    engine = at_reviews("any")
    grant = engine.claim("box", ["design/triage"])
    assert grant is not None

    finish(engine, SECURITY)

    assert engine.get(TOP).state == "merging"
    assert engine.get(DESIGN).status == "cancelled"
    assert [branch["workflow"] for branch in joined(engine)] == ["security"]
    assert engine.heartbeat("box", [grant.task]) == [grant.task]  # its task was cancelled
    cancelled = next(e for e in engine.history(DESIGN) if e.kind == "cancelled")
    assert cancelled.data == {"reason": "the parent left 'reviews' on 'joined'"}


def test_a_parent_that_fails_tells_its_children_why() -> None:
    document = parallel_document()
    document["transitions"] = [
        edge for edge in document["transitions"]
        if not (edge["from"] == "reviews" and edge.get("on") == "timeout")
    ]  # fmt: skip
    clock = Clock()
    engine = new(document, clock)
    engine.start(TOP, {"repo": "acme/api"})
    engine.fire(TOP, "done", None)

    clock.advance(hours=49)  # past the timeout of `reviews`, which no transition takes
    engine.tick()

    assert engine.get(TOP).status == "failed"
    reason = engine.history(TOP)[-1].data["reason"]
    assert reason.startswith("'reviews' timed out")
    for child in (SECURITY, DESIGN):
        cancelled = next(e for e in engine.history(child) if e.kind == "cancelled")
        assert cancelled.data == {"reason": f"the parent failed: {reason}"}


def test_leaving_a_parallel_state_early_cancels_the_live_children() -> None:
    clock = Clock()
    engine = at_reviews(clock=clock)

    clock.advance(hours=49)  # past the timeout of `reviews`, two days
    engine.tick()

    assert engine.get(TOP).state == "blocked"
    assert engine.get(DESIGN).status == "cancelled"
    cancelled = next(e for e in engine.history(DESIGN) if e.kind == "cancelled")
    assert cancelled.data == {"reason": "the parent left 'reviews' on 'timeout'"}


def test_a_join_that_no_transition_takes_fails_the_parent() -> None:
    document = parallel_document()
    document["transitions"] = [
        edge for edge in document["transitions"]
        if not (edge["from"] == "reviews" and edge.get("otherwise"))
    ]  # fmt: skip
    engine = new(document)
    engine.start(TOP, {"repo": "acme/api"})
    engine.fire(TOP, "done", None)
    finish(engine, SECURITY)

    finish(engine, DESIGN, "deny", {"count": 1})

    token = engine.get(TOP)
    assert (token.state, token.status) == ("reviews", "failed")
    failed = engine.history(TOP)[-1]
    assert failed.kind == "failed"
    assert failed.data["reason"].startswith(
        "the join of 'reviews' cannot move the token: no guard matched 'joined'"
    )


def test_children_of_children_join_up_through_each_level() -> None:
    engine = new(nested_document())
    engine.start(TOP, {})
    middle = child_id(TOP, "outer", 1, "middle")
    leaf = child_id(middle, "inner", 1, "leaf")

    assert (engine.get(middle).parent, engine.get(leaf).parent) == (TOP, middle)
    assert engine.claim("box", ["middle/inner"]) is None
    grant = engine.claim("box", ["leaf/work"])
    assert grant is not None and grant.token == leaf
    engine.report(grant.task, Outcome(kind="event", event="ok"), supervisor="box")

    assert [engine.get(t).status for t in (leaf, middle, TOP)] == ["completed"] * 3


# ── an entry that cannot start its children is refused ───────────────────


def test_an_entry_whose_child_cannot_start_is_refused() -> None:
    engine = new(without_context())
    engine.start(TOP, {"repo": 5})

    with pytest.raises(
        EventNotAllowed, match="the child 'security' of 'reviews' cannot start: its context"
    ):
        engine.fire(TOP, "done", None)

    assert engine.get(TOP).state == "coding"
    assert engine.history(TOP)[-1].kind == "event_rejected"
    assert engine.tokens(parent=TOP) == []


def test_a_token_that_starts_in_a_parallel_state_whose_child_cannot_start_is_refused() -> None:
    document = without_context()
    document["transitions"][0] = {"from": "__start__", "to": "reviews"}
    engine = new(document)

    with pytest.raises(InvalidContext, match="the child 'security' of 'reviews' cannot start"):
        engine.start(TOP, {"repo": 5})

    assert engine.tokens() == []


def test_a_child_id_that_another_token_has_refuses_the_entry() -> None:
    engine = new(parallel_document())
    engine.start(TOP, {"repo": "acme/api"})
    engine.start("t-1.REVIEWS.1.security", {"repo": "acme/other"})

    with pytest.raises(EventNotAllowed, match=f"the child id {SECURITY!r} is taken"):
        engine.fire(TOP, "done", None)

    assert engine.get(TOP).state == "coding"


def test_a_child_id_longer_than_128_characters_refuses_the_entry() -> None:
    engine = new(parallel_document())
    long = "T" + "x" * 110  # its child ids have 130 characters
    engine.start(long, {"repo": "acme/api"})

    with pytest.raises(EventNotAllowed, match="would have an id longer than 128 characters"):
        engine.fire(long, "done", None)

    assert engine.get(long).state == "coding"


def test_a_child_that_ends_as_it_starts_can_meet_the_join_while_the_parent_enters() -> None:
    """A hand-made graph file can lead a child from START to END: the child ends as it
    starts. With `any`, the join is met before the next child starts. The parent goes
    on at once, starts no more children, and keeps no timeout of `reviews`."""
    document = parallel_document("any")
    document["workflows"]["security"]["transitions"][0] = {"from": "__start__", "to": "__end__"}
    clock = Clock()
    engine = new(document, clock)
    engine.start(TOP, {"repo": "acme/api"})

    engine.fire(TOP, "done", None)

    assert engine.get(TOP).state == "coding"  # `final` is not 'approved': the otherwise
    assert [t.token for t in engine.tokens(parent=TOP)] == [SECURITY]
    clock.advance(hours=49)
    engine.tick()
    assert (engine.get(TOP).state, engine.get(TOP).status) == ("coding", "active")
