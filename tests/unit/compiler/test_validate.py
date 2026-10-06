"""Whole-graph checks: they run when compiling, and report every problem at once."""

import pytest
from pydantic import BaseModel, create_model

from stateous import END, START, Workflow
from stateous.compiler.validate import validate
from stateous.model.workflow import DefinitionError


class Ticket(BaseModel):
    repo: str
    brief: str | None = None
    points: int = 0
    priority: str = "low"


def refused(flow: Workflow) -> str:
    with pytest.raises(DefinitionError) as raised:
        validate(flow)
    return str(raised.value)


def test_a_workflow_needs_states() -> None:
    assert "workflow 'empty' has no states" in refused(Workflow("empty"))


def test_a_workflow_needs_a_way_out_of_start() -> None:
    flow = Workflow("loop")
    a = flow.state("a")
    flow.transition(a, END, on=flow.event("done"))

    assert "'loop' has no transition from START" in refused(flow)


def test_a_workflow_needs_a_way_into_end() -> None:
    flow = Workflow("loop")
    a = flow.state("a")
    flow.transition(START, a)
    flow.transition(a, a, on=flow.event("again"))

    assert "'loop' has no transition into END" in refused(flow)


def test_every_state_must_be_reachable_from_start() -> None:
    flow = Workflow("sdlc")
    a, orphan = flow.state("a"), flow.state("orphan")
    flow.transition(START, a)
    flow.transition(a, END, on=flow.event("done"))
    flow.transition(orphan, a, on=flow.event("go"))

    problem = refused(flow)

    assert "state 'orphan' cannot be reached from START" in problem
    assert "'a'" not in problem


def test_the_first_state_declared_is_not_special() -> None:
    flow = Workflow("sdlc")
    flow.state("first")
    second = flow.state("second")
    flow.transition(START, second)
    flow.transition(second, END, on=flow.event("done"))

    assert "'first' cannot be reached from START" in refused(flow)


def test_an_unguarded_transition_next_to_guards_must_say_otherwise() -> None:
    flow = Workflow("sdlc")
    testing, fixing, blocked = flow.state("testing"), flow.state("fixing"), flow.state("blocked")
    failed, passed = flow.event("failed"), flow.event("passed")
    flow.transition(START, testing)
    flow.transition(testing, fixing, on=failed, when="count('failed') < 3")
    flow.transition(testing, blocked, on=failed)
    flow.transition(testing, END, on=passed)
    flow.transition(fixing, testing, on=passed)
    flow.transition(blocked, testing, on=passed)

    assert "mark the unguarded one otherwise=True" in refused(flow)


def test_a_guarded_group_without_a_fallback_compiles() -> None:
    # stateous-flow's rule (contract change 1): at run time, an event no guard
    # matches is refused.
    flow = Workflow("sdlc", context=Ticket)
    testing = flow.state("testing")
    failed = flow.event("failed")
    flow.transition(START, testing)
    flow.transition(testing, testing, on=failed, when="count('failed') < 3")
    flow.transition(testing, END, on=flow.event("passed"))

    validate(flow)


def test_a_guard_may_only_read_context_keys_the_model_has() -> None:
    flow = Workflow("sdlc", context=Ticket)
    start = flow.state("start")
    flow.transition(START, start)
    flow.transition(start, END, on=flow.event("go"), when="ctx['pr_number'] == null")

    assert "reads ctx['pr_number'], which Ticket does not have" in refused(flow)


def test_without_a_context_model_a_guard_may_read_any_key() -> None:
    flow = Workflow("sdlc")
    start = flow.state("start")
    flow.transition(START, start)
    flow.transition(start, END, on=flow.event("go"), when="ctx['anything'] == 1")

    validate(flow)


def test_a_guard_may_only_count_events_that_exist() -> None:
    flow = Workflow("sdlc")
    start = flow.state("start", timeout="1h")
    flow.transition(START, start)
    flow.transition(start, END, on=flow.event("go"), when="count('faild') < 3")
    flow.transition(start, END, on="timeout", when="count('timeout') > 2")

    problem = refused(flow)

    assert "counts 'faild', which is not an event" in problem
    assert "'timeout', which" not in problem


def test_two_models_with_one_name_are_refused() -> None:
    first = create_model("Report", passed=(int, ...))
    second = create_model("Report", failed=(int, ...))
    flow = Workflow("sdlc")
    a = flow.state("a")
    flow.transition(START, a)
    flow.transition(a, END, on=flow.event("passed", payload=first))
    flow.transition(a, END, on=flow.event("failed", payload=second))

    assert "two different models are named 'Report'" in refused(flow)


def test_start_guards_must_be_exclusive() -> None:
    flow = Workflow("sdlc", context=Ticket)
    a, b = flow.state("a"), flow.state("b")
    flow.transition(START, a, when="ctx['points'] > 5")
    flow.transition(START, b, when="ctx['points'] > 3")
    done = flow.event("done")
    flow.transition(a, END, on=done)
    flow.transition(b, END, on=done)

    assert "START: the guards \"ctx['points'] > 5\" and \"ctx['points'] > 3\" can both" in (
        refused(flow)
    )


def test_exclusive_start_guards_compile() -> None:
    flow = Workflow("sdlc", context=Ticket)
    a, b = flow.state("a"), flow.state("b")
    flow.transition(START, a, when="ctx['points'] > 5")
    flow.transition(START, b, when="ctx['points'] <= 5")
    done = flow.event("done")
    flow.transition(a, END, on=done)
    flow.transition(b, END, on=done)

    validate(flow)


def test_a_start_may_have_no_fallback() -> None:
    flow = Workflow("sdlc", context=Ticket)
    urgent = flow.state("urgent")
    flow.transition(START, urgent, when="ctx['priority'] == 'high'")
    flow.transition(urgent, END, on=flow.event("done"))

    validate(flow)  # the caller of `start` is told when no guard holds


def test_all_problems_are_reported_together() -> None:
    flow = Workflow("sdlc")
    flow.state("start")
    flow.state("orphan")

    problem = refused(flow)

    assert problem.startswith("workflow 'sdlc' is not valid:\n  - ")
    assert "cannot be reached" in problem
    assert "no transition from START" in problem
    assert "no transition into END" in problem
