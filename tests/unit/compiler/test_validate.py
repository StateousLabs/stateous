"""Whole-graph checks: they run when compiling, and report every problem at once."""

import pytest
from pydantic import BaseModel, create_model

from stateous import END, START, Workflow
from stateous.compiler.validate import TOP_ID_ROOM, nested_workflows, validate
from stateous.model.workflow import DefinitionError, State

from ...parallel import Review, parallel_workflow, review


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


# ── child workflows ────────────────────────────────────


def with_child(child: Workflow, *, inputs: tuple[str, ...] = ("repo",)) -> Workflow:
    """`sdlc`, whose one state runs `child` and ends on `joined`."""
    flow = Workflow("sdlc", context=Ticket)
    reviews = flow.state("reviews", parallel=[child], inputs=inputs)
    flow.transition(START, reviews)
    flow.transition(reviews, END, on="joined")
    return flow


def test_the_design_example_is_valid() -> None:
    flow = parallel_workflow()

    validate(flow)

    assert [child.name for child in nested_workflows(flow)] == ["security", "design"]


def test_a_child_workflow_gets_the_same_checks() -> None:
    child = Workflow("review")
    child.state("waiting")

    problem = refused(with_child(child, inputs=()))

    assert "in child workflow 'review': workflow 'review' has no transition from START" in problem


def test_a_child_workflows_start_needs_a_fallback() -> None:
    child = Workflow("review", context=Ticket)
    urgent, calm = child.state("urgent"), child.state("calm")
    child.transition(START, urgent, when="ctx['priority'] == 'high'")
    child.transition(START, calm, when="ctx['priority'] == 'low'")
    done = child.event("done")
    child.transition(urgent, END, on=done)
    child.transition(calm, END, on=done)

    problem = refused(with_child(child))

    assert (
        "in child workflow 'review': START has no fallback: a child workflow is started by "
        "the state that runs it"
    ) in problem


def test_a_child_workflows_start_with_a_fallback_is_valid() -> None:
    child = Workflow("review", context=Ticket)
    urgent, calm = child.state("urgent"), child.state("calm")
    child.transition(START, urgent, when="ctx['priority'] == 'high'")
    child.transition(START, calm, otherwise=True)
    done = child.event("done")
    child.transition(urgent, END, on=done)
    child.transition(calm, END, on=done)

    validate(with_child(child))


def test_guards_in_a_child_workflow_are_checked_too() -> None:
    child = Workflow("review", context=Ticket)
    waiting = child.state("waiting")
    child.transition(START, waiting)
    child.transition(waiting, END, on=child.event("done"), when="ctx['points'] > 5")
    child.transition(waiting, END, on="done", when="ctx['points'] > 3")

    assert "in child workflow 'review': 'done' from 'waiting': the guards" in refused(
        with_child(child)
    )


def test_inputs_must_give_each_required_field_of_a_childs_context() -> None:
    problem = refused(with_child(review("security"), inputs=("repo",)))

    assert (
        "state 'reviews' does not hand on 'branch', which 'security' requires; add it to inputs"
    ) in problem


def test_inputs_must_be_keys_of_the_parents_context() -> None:
    problem = refused(with_child(review("security"), inputs=("repo", "branch", "nothing")))

    assert "state 'reviews' hands on 'branch', which Ticket does not have" in problem
    assert "state 'reviews' hands on 'nothing', which Ticket does not have" in problem


def test_a_workflow_cannot_run_itself_further_down() -> None:
    top, middle = Workflow("top"), Workflow("middle")
    outer = top.state("outer", parallel=[middle])
    top.transition(START, outer)
    top.transition(outer, END, on="joined")
    inner = middle.state("inner", parallel=[top])
    middle.transition(START, inner)
    middle.transition(inner, END, on="joined")

    problem = refused(top)

    assert "state 'inner' of 'middle' runs 'top', which runs 'middle' in turn" in problem


def test_two_different_workflows_with_one_name_are_refused() -> None:
    flow = Workflow("sdlc", context=Review)
    first = flow.state("first", parallel=[review("check")], inputs=("repo", "branch"))
    second = flow.state("second", parallel=[review("check")], inputs=("repo", "branch"))
    flow.transition(START, first)
    flow.transition(first, second, on="joined")
    flow.transition(second, END, on="joined")

    assert "two different workflows are named 'check'" in refused(flow)


def test_one_child_workflow_in_two_states_is_valid() -> None:
    check = review("check")
    flow = Workflow("sdlc", context=Review)
    first = flow.state("first", parallel=[check], inputs=("repo", "branch"))
    second = flow.state("second", parallel=[check], inputs=("repo", "branch"))
    flow.transition(START, first)
    flow.transition(first, second, on="joined")
    flow.transition(second, END, on="joined")

    validate(flow)

    assert nested_workflows(flow) == [check]


def test_a_nesting_that_leaves_too_little_room_for_the_top_id_is_refused() -> None:
    child = Workflow("c" * 40, context=Review)
    waiting = child.state("waiting")
    child.transition(START, waiting)
    child.transition(waiting, END, on=child.event("done"))
    flow = Workflow("sdlc", context=Review)
    long_state = flow.state("s" * 30, parallel=[child], inputs=("repo", "branch"))
    flow.transition(START, long_state)
    flow.transition(long_state, END, on="joined")

    problem = refused(flow)

    # "." + 30 + "." + 3 (the visit) + "." + 40 = 76, more than 128 - 64.
    assert "the child token ids can be 76 characters longer than the top token's id" in problem
    assert f"a top id of {TOP_ID_ROOM} characters" in problem


def test_two_models_with_one_name_in_a_parent_and_a_child_are_refused() -> None:
    other = create_model("Ticket", repo=(str, ...))
    child = Workflow("review", context=other)
    waiting = child.state("waiting")
    child.transition(START, waiting)
    child.transition(waiting, END, on=child.event("done"))

    assert "two different models are named 'Ticket'" in refused(with_child(child))


def test_a_guard_on_joined_may_count_joined() -> None:
    flow = Workflow("sdlc", context=Review)
    reviews = flow.state("reviews", parallel=[review("security")], inputs=("repo", "branch"))
    flow.transition(START, reviews)
    flow.transition(reviews, END, on="joined", when="count('joined') >= 2")
    flow.transition(reviews, reviews, on="joined", otherwise=True)

    validate(flow)


# ── a parallel state's own exits ───────────────────────


def test_a_parallel_state_without_a_transition_on_joined_is_refused() -> None:
    flow = Workflow("sdlc", context=Review)
    reviews = flow.state("reviews", parallel=[review("security")], inputs=("repo", "branch"))
    flow.transition(START, reviews)
    flow.transition(reviews, END, on="timeout")

    assert "parallel state 'reviews' needs a transition on 'joined'" in refused(flow)


def test_a_parallel_state_with_a_transition_on_error_is_refused() -> None:
    flow = Workflow("sdlc", context=Review)
    reviews = flow.state("reviews", parallel=[review("security")], inputs=("repo", "branch"))
    flow.transition(START, reviews)
    flow.transition(reviews, END, on="joined")
    flow.transition(reviews, END, on="error")

    problem = refused(flow)

    assert (
        "parallel state 'reviews' has no task, so 'error' can never fire; "
        "check 'branches' on 'joined'"
    ) in problem


def nested_in_review() -> tuple[Workflow, Workflow, State]:
    """`sdlc`, whose child `review` has its own parallel state `inner`, running
    `security`. Both parent and child pass `repo` and `branch` along."""
    child = Workflow("review", context=Review)
    inner = child.state("inner", parallel=[review("security")], inputs=("repo", "branch"))
    child.transition(START, inner)
    flow = Workflow("sdlc", context=Review)
    reviews = flow.state("reviews", parallel=[child], inputs=("repo", "branch"))
    flow.transition(START, reviews)
    flow.transition(reviews, END, on="joined")
    return flow, child, inner


def test_a_parallel_state_in_a_child_workflow_also_needs_joined() -> None:
    flow, child, inner = nested_in_review()
    child.transition(inner, END, on="timeout")

    problem = refused(flow)

    assert (
        "in child workflow 'review': parallel state 'inner' needs a transition on 'joined'"
    ) in problem


def test_a_parallel_state_in_a_child_workflow_also_refuses_error() -> None:
    flow, child, inner = nested_in_review()
    child.transition(inner, END, on="joined")
    child.transition(inner, END, on="error")

    problem = refused(flow)

    assert (
        "in child workflow 'review': parallel state 'inner' has no task, so 'error' can "
        "never fire; check 'branches' on 'joined'"
    ) in problem
