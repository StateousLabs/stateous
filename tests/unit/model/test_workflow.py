"""Declaring a workflow: states, events, transitions, START and END."""

import re

import pytest
from pydantic import BaseModel

from stateous import END, START, Workflow
from stateous.model.workflow import JOINED, RESERVED_EVENTS, DefinitionError

from ...parallel import ALL_APPROVED, parallel_workflow, review


class Report(BaseModel):
    passed: int
    failed: int


class Findings(BaseModel):
    severity: str


class Ticket(BaseModel):
    priority: str = "low"


# ── names ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["a", "in-progress", "step_2", "a" * 64])
def test_a_name_is_lower_case_letters_digits_underscores_and_hyphens(name: str) -> None:
    flow = Workflow(name)

    assert flow.state(name).name == name
    assert flow.event(name).name == name


@pytest.mark.parametrize("name", ["Testing", "1st", "", "in progress", "a" * 65, "état", "a.b"])
def test_any_other_name_is_refused(name: str) -> None:
    flow = Workflow("sdlc")

    with pytest.raises(DefinitionError, match="is not valid: use a-z, 0-9"):
        flow.state(name)
    with pytest.raises(DefinitionError, match="is not valid: use a-z, 0-9"):
        flow.event(name)
    with pytest.raises(DefinitionError, match=f"workflow name {re.escape(repr(name))}"):
        Workflow(name)


# ── states ────────────────────────────────────────────────────────────────


def test_a_state_is_declared_once() -> None:
    flow = Workflow("sdlc")
    testing = flow.state("testing", timeout="1h", retries=2)

    assert (testing.name, testing.workflow, testing.timeout, testing.retries) == (
        "testing",
        "sdlc",
        "1h",
        2,
    )
    with pytest.raises(DefinitionError, match="state 'testing' is already declared"):
        flow.state("testing")


def test_a_timeout_is_a_duration() -> None:
    flow = Workflow("sdlc")

    assert flow.state("slow", timeout="3650d").timeout == "3650d"
    with pytest.raises(DefinitionError, match="state 'a': 'an hour' is not a duration"):
        flow.state("a", timeout="an hour")
    with pytest.raises(DefinitionError, match="'3651d' is too long"):
        flow.state("b", timeout="3651d")
    with pytest.raises(DefinitionError, match="60 is not a duration"):
        flow.state("c", timeout=60)  # type: ignore[arg-type]


@pytest.mark.parametrize("retries", [-1, True, 1.5, "2"])
def test_retries_is_a_whole_number_of_at_least_zero(retries: object) -> None:
    flow = Workflow("sdlc")

    with pytest.raises(DefinitionError, match="retries must be a whole number, 0 or more"):
        flow.state("testing", retries=retries)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["__start__", "__end__"])
def test_a_state_may_not_take_the_name_of_start_or_end(name: str) -> None:
    with pytest.raises(DefinitionError, match=f"{name!r} is Stateous's own"):
        Workflow("sdlc").state(name)


# ── events ────────────────────────────────────────────────────────────────


def test_an_event_is_declared_once_with_its_payload() -> None:
    flow = Workflow("sdlc")
    failed = flow.event("failed", payload=Report)

    assert (failed.name, failed.payload, failed.workflow) == ("failed", Report, "sdlc")
    with pytest.raises(DefinitionError, match="event 'failed' is already declared"):
        flow.event("failed")


@pytest.mark.parametrize("name", ["timeout", "error", "joined", "__started__"])
def test_stateous_own_events_are_never_declared(name: str) -> None:
    with pytest.raises(DefinitionError, match=f"event {name!r} is Stateous's own"):
        Workflow("sdlc").event(name)


def test_a_payload_and_a_context_are_pydantic_models() -> None:
    with pytest.raises(DefinitionError, match="payload must be a pydantic model class"):
        Workflow("sdlc").event("done", payload=dict)  # type: ignore[arg-type]
    with pytest.raises(DefinitionError, match="context must be a pydantic model class"):
        Workflow("sdlc", context=Report(passed=1, failed=0))  # type: ignore[arg-type]


# ── transitions ───────────────────────────────────────────────────────────


def test_a_transition_connects_states_of_its_own_workflow() -> None:
    flow = Workflow("sdlc")
    testing, merging = flow.state("testing"), flow.state("merging")
    passed = flow.event("passed")

    edge = flow.transition(testing, merging, on=passed)

    assert (edge.source, edge.target, edge.event) == (testing, merging, passed)
    assert flow.transitions == [edge]


def test_a_transition_refuses_a_state_of_another_workflow() -> None:
    flow, other = Workflow("sdlc"), Workflow("other")
    testing, foreign = flow.state("testing"), other.state("merging")

    with pytest.raises(DefinitionError, match="state 'merging' belongs to workflow 'other'"):
        flow.transition(testing, foreign, on=flow.event("passed"))


@pytest.mark.parametrize("name", ["timeout", "error"])
def test_stateous_own_events_are_used_by_name(name: str) -> None:
    flow = Workflow("sdlc")
    testing = flow.state("testing", timeout="1h")

    edge = flow.transition(testing, END, on=name)

    assert (edge.event.name, edge.event.workflow, edge.event.payload) == (name, None, None)


def test_an_undeclared_event_name_is_refused() -> None:
    flow = Workflow("sdlc")
    testing = flow.state("testing")

    with pytest.raises(DefinitionError, match="event 'pased' is not declared"):
        flow.transition(testing, testing, on="pased")


def test_a_guard_is_parsed_when_the_transition_is_declared() -> None:
    flow = Workflow("sdlc")
    testing, fixing = flow.state("testing"), flow.state("fixing")
    failed = flow.event("failed", payload=Report)

    edge = flow.transition(testing, fixing, on=failed, when="count('failed') < 3")

    assert edge.when is not None
    assert edge.when.events == {"failed"}


def test_a_malformed_guard_is_a_definition_error() -> None:
    flow = Workflow("sdlc")
    testing = flow.state("testing")
    failed = flow.event("failed", payload=Report)

    with pytest.raises(DefinitionError, match="arithmetic is not part of the guard language"):
        flow.transition(testing, testing, on=failed, when="count('failed') + 1 < 3")


def test_a_guard_may_only_read_fields_the_payload_has() -> None:
    flow = Workflow("security-review")
    scan, rejected = flow.state("scan"), flow.state("rejected")
    found = flow.event("findings", payload=Findings)

    with pytest.raises(DefinitionError, match="'findings' payload has no field 'severty'"):
        flow.transition(scan, rejected, on=found, when="payload.severty == 'high'")


def test_a_guard_on_timeout_has_no_payload_to_read() -> None:
    flow = Workflow("sdlc")
    testing = flow.state("testing", timeout="1h")

    with pytest.raises(DefinitionError, match="'timeout' payload has no field 'x'"):
        flow.transition(testing, END, on="timeout", when="payload.x == 1")


def test_otherwise_cannot_also_have_a_guard() -> None:
    flow = Workflow("sdlc")
    testing = flow.state("testing")
    failed = flow.event("failed")

    with pytest.raises(
        DefinitionError, match=re.escape("a fallback (otherwise=True) has no guard")
    ):
        flow.transition(testing, testing, on=failed, when="count('failed') > 1", otherwise=True)


def test_two_unguarded_transitions_on_one_event_are_ambiguous() -> None:
    flow = Workflow("sdlc")
    testing, fixing, blocked = flow.state("testing"), flow.state("fix"), flow.state("blocked")
    failed = flow.event("failed")
    flow.transition(testing, fixing, on=failed)

    with pytest.raises(DefinitionError, match="'failed' from 'testing' is ambiguous"):
        flow.transition(testing, blocked, on=failed)


def test_at_most_one_fallback_on_one_state_and_event() -> None:
    flow = Workflow("sdlc")
    testing, fixing, blocked = flow.state("testing"), flow.state("fix"), flow.state("blocked")
    failed = flow.event("failed")
    flow.transition(testing, fixing, on=failed, otherwise=True)

    with pytest.raises(DefinitionError, match="is ambiguous"):
        flow.transition(testing, blocked, on=failed, otherwise=True)


# ── START and END ─────────────────────────────────────────────────────────


def test_start_and_end_are_imported_never_declared() -> None:
    flow = Workflow("sdlc")
    testing = flow.state("testing")

    begin = flow.transition(START, testing)
    end = flow.transition(testing, END, on=flow.event("passed"))

    assert (begin.source, end.target) == (START, END)
    assert list(flow.states) == ["testing"]
    assert (START.name, END.name) == ("__start__", "__end__")


def test_a_transition_from_start_is_taken_on_started() -> None:
    flow = Workflow("sdlc")

    edge = flow.transition(START, flow.state("blocked"))

    assert (edge.event.name, edge.event.workflow) == ("__started__", None)


def test_a_transition_from_start_takes_no_event() -> None:
    flow = Workflow("sdlc")
    blocked, go = flow.state("blocked"), flow.event("go")

    with pytest.raises(DefinitionError, match="a transition from START takes no `on=`"):
        flow.transition(START, blocked, on=go)


def test_every_other_transition_needs_an_event() -> None:
    flow = Workflow("sdlc")
    testing = flow.state("testing")

    with pytest.raises(DefinitionError, match="from 'testing' needs `on=`"):
        flow.transition(testing, END)


def test_nothing_leads_into_start_and_nothing_leaves_end() -> None:
    flow = Workflow("sdlc")
    testing, again = flow.state("testing"), flow.event("again")

    with pytest.raises(DefinitionError, match="nothing may lead into START"):
        flow.transition(testing, START, on=again)
    with pytest.raises(DefinitionError, match="nothing may leave END"):
        flow.transition(END, testing, on=again)


def test_start_does_not_lead_straight_to_end() -> None:
    with pytest.raises(DefinitionError, match="straight from START to END"):
        Workflow("sdlc").transition(START, END)


def test_started_is_only_for_start() -> None:
    flow = Workflow("sdlc")
    a, b = flow.state("a"), flow.state("b")

    with pytest.raises(DefinitionError, match="'__started__' is Stateous's own"):
        flow.transition(a, b, on="__started__")


def test_start_guards_read_the_context() -> None:
    flow = Workflow("sdlc", context=Ticket)
    urgent, queued = flow.state("urgent"), flow.state("queued")

    flow.transition(START, urgent, when="ctx['priority'] == 'high'")
    flow.transition(START, queued, otherwise=True)

    assert [edge.target for edge in flow.transitions] == [urgent, queued]


def test_a_start_guard_cannot_read_a_payload_or_count() -> None:
    flow = Workflow("sdlc")
    urgent = flow.state("urgent")

    with pytest.raises(DefinitionError, match="START's guards read the context only"):
        flow.transition(START, urgent, when="payload.priority == 'high'")
    with pytest.raises(DefinitionError, match="nothing has happened yet"):
        flow.transition(START, urgent, when="count('go') > 0")


def test_two_unguarded_ways_out_of_start_are_ambiguous() -> None:
    flow = Workflow("sdlc")
    a, b = flow.state("a"), flow.state("b")
    flow.transition(START, a)

    with pytest.raises(DefinitionError, match="START is ambiguous: two transitions"):
        flow.transition(START, b)


# ── parallel states ────────────────────────────────────


def test_a_parallel_state_runs_child_workflows_and_joins() -> None:
    security, design = review("security"), review("design")
    flow = Workflow("sdlc")

    reviews = flow.state("reviews", parallel=[security, design], join="any", inputs=["repo"])

    assert reviews.parallel == (security, design)
    assert (reviews.join, reviews.inputs) == ("any", ("repo",))
    assert flow.state("coding").parallel == ()


def test_joined_is_stateous_own_event() -> None:
    assert JOINED == "joined"
    assert JOINED in RESERVED_EVENTS


def test_a_join_is_all_or_any() -> None:
    with pytest.raises(DefinitionError, match="join must be 'all' or 'any', not 'most'"):
        Workflow("sdlc").state("reviews", parallel=[review("a")], join="most")


def test_a_parallel_state_has_no_retries() -> None:
    with pytest.raises(DefinitionError, match="a parallel state has no task, so it has no retries"):
        Workflow("sdlc").state("reviews", parallel=[review("a")], retries=1)


def test_a_parallel_state_runs_each_workflow_once() -> None:
    twice = review("a")

    with pytest.raises(DefinitionError, match="runs the workflow 'a' two times"):
        Workflow("sdlc").state("reviews", parallel=[twice, review("a")])


def test_a_workflow_cannot_run_itself() -> None:
    flow = Workflow("sdlc")

    with pytest.raises(DefinitionError, match="a workflow cannot run itself"):
        flow.state("reviews", parallel=[flow])


@pytest.mark.parametrize("given", [review("a"), ["a"]])
def test_parallel_takes_a_list_of_workflows(given: object) -> None:
    with pytest.raises(DefinitionError, match="parallel takes a list of workflows"):
        Workflow("sdlc").state("reviews", parallel=given)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(("join", "inputs"), [("any", ()), ("all", ("repo",))])
def test_join_and_inputs_need_parallel(join: str, inputs: tuple[str, ...]) -> None:
    with pytest.raises(DefinitionError, match="join and inputs are for a parallel state"):
        Workflow("sdlc").state("coding", join=join, inputs=inputs)


def test_inputs_is_a_list_of_context_keys() -> None:
    with pytest.raises(DefinitionError, match=r"inputs is a list of context keys, e\.g\."):
        Workflow("sdlc").state("reviews", parallel=[review("a")], inputs="repo")


def test_only_a_parallel_state_is_left_on_joined() -> None:
    flow = Workflow("sdlc")
    coding = flow.state("coding")

    with pytest.raises(DefinitionError, match="only a parallel state is left on 'joined'"):
        flow.transition(coding, END, on="joined")


def test_a_parallel_state_is_left_on_joined_with_a_guard_on_its_children() -> None:
    flow = parallel_workflow()

    joins = [edge for edge in flow.transitions if edge.event.name == JOINED]

    assert [edge.target.name for edge in joins] == ["merging", "coding"]
    assert joins[0].when is not None and joins[0].when.branches
    assert joins[0].when.expression == ALL_APPROVED


def test_branches_are_read_only_on_joined() -> None:
    flow = Workflow("sdlc")
    coding = flow.state("coding")
    done = flow.event("done")

    with pytest.raises(DefinitionError, match=r"branches.all\(\) and branches.any\(\) are for"):
        flow.transition(coding, END, on=done, when="branches.all(final == 'x')")


def test_a_guard_on_joined_reads_no_payload_field_outside_branches() -> None:
    flow = Workflow("sdlc")
    reviews = flow.state("reviews", parallel=[review("a")])

    with pytest.raises(DefinitionError, match="'joined' payload has no field 'count'"):
        flow.transition(reviews, END, on="joined", when="payload.count > 1")
