"""The engine keeps an event's payload in its `into=` field (R1). It does so in the
same step that moves the token, before the guards run, for every source of the
event: `fire`, a routine's report, a timer, a join, and a resume."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from stateous import END, START, Branches, Workflow
from stateous.compiler.compile import compile_workflow
from stateous.control.engine import Engine
from stateous.control.graph import Graph
from stateous.control.store import Store
from stateous.model.wire import Outcome, Schedule

from ...parallel import DESIGN, SECURITY, review
from .support import Clock, kinds

T = "T-1"
ANN = {"by": "ann"}


class Approval(BaseModel):
    by: str


class Ticket(BaseModel):
    repo: str
    branch: str = "main"
    approval: Approval | None = None
    reviews: Branches | None = None


def approvals() -> dict[str, Any]:
    """`waiting` -> `checked` on `approved` when the kept approval is there; else END."""
    flow = Workflow("sdlc", context=Ticket)
    waiting = flow.state("waiting")
    checked = flow.state("checked")
    approved = flow.event("approved", payload=Approval, into="approval")
    flow.transition(START, waiting)
    flow.transition(waiting, checked, on=approved, when="ctx['approval'] != null")
    flow.transition(waiting, END, on=approved, otherwise=True)
    flow.transition(checked, END, on=flow.event("done"))
    return compile_workflow(flow)


def reviews(join: str) -> dict[str, Any]:
    flow = Workflow("sdlc", context=Ticket)
    state = flow.state(
        "reviews",
        parallel=[review("security"), review("design")],
        join=join,
        inputs=["repo", "branch"],
        into="reviews",
    )
    flow.transition(START, state)
    flow.transition(state, END, on="joined")
    return compile_workflow(flow)


def engine_of(document: dict[str, Any], clock: Clock | None = None) -> Engine:
    engine = Engine(Store("sqlite://"), Graph(document), clock=clock or Clock())
    engine.start(T, {"repo": "acme/api"})
    return engine


def test_fire_keeps_the_payload_in_the_context() -> None:
    engine = engine_of(approvals())

    engine.fire(T, "approved", ANN)

    assert engine.get(T).context["approval"] == ANN
    happened = kinds(engine, T)
    assert happened.index("context_changed") < len(happened) - 1 - happened[::-1].index(
        "event_accepted"
    )
    changed = next(e for e in engine.history(T) if e.kind == "context_changed")
    assert changed.data["keys"] == ["approval"]


def test_a_guard_reads_the_kept_payload() -> None:
    engine = engine_of(approvals())

    engine.fire(T, "approved", ANN)

    assert engine.get(T).state == "checked"


def test_a_report_keeps_the_payload_and_it_wins_over_the_routine() -> None:
    engine = engine_of(approvals())
    grant = engine.claim("box", ["waiting"])
    assert grant is not None

    outcome = Outcome(
        kind="event", event="approved", payload=ANN, context={"approval": {"by": "x"}}
    )
    engine.report(grant.task, outcome, supervisor="box")

    assert engine.get(T).context["approval"] == ANN


def test_a_timer_keeps_the_payload() -> None:
    clock = Clock()
    engine = engine_of(approvals(), clock)
    grant = engine.claim("box", ["waiting"])
    assert grant is not None
    timer = Schedule(event="approved", payload=ANN, after="1s")
    engine.report(grant.task, Outcome(kind="waiting", schedules=(timer,)), supervisor="box")

    clock.advance(seconds=2)
    engine.tick()

    assert engine.get(T).context["approval"] == ANN


def test_a_paused_token_keeps_the_payload_on_resume() -> None:
    engine = engine_of(approvals())
    engine.pause(T)

    engine.fire(T, "approved", ANN)
    assert engine.get(T).context.get("approval") is None

    engine.resume(T)
    assert engine.get(T).context["approval"] == ANN


def test_a_join_keeps_the_branches_in_the_context() -> None:
    engine = engine_of(reviews("all"))

    for child in (SECURITY, DESIGN):
        engine.fire(child, "lgtm", None)
        engine.fire(child, "recorded", None)

    kept = Branches.model_validate(engine.get(T).context["reviews"])
    assert sorted(branch.token for branch in kept.branches) == [DESIGN, SECURITY]
    assert all(branch.context["repo"] == "acme/api" for branch in kept.branches)


def test_a_join_of_any_keeps_one_branch() -> None:
    engine = engine_of(reviews("any"))

    engine.fire(SECURITY, "lgtm", None)
    engine.fire(SECURITY, "recorded", None)

    kept = Branches.model_validate(engine.get(T).context["reviews"])
    assert [branch.token for branch in kept.branches] == [SECURITY]
