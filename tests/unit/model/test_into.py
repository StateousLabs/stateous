"""`into=`: the context field that keeps an event's payload, or a join's branches (R1).
It is checked when you declare it, it is in the graph file, and the runtime graph
names it for each state and event."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import BaseModel

from stateous import END, START, Branches, Workflow
from stateous.compiler.compile import compile_workflow
from stateous.control.graph import Graph
from stateous.model.graph_file import GraphFileError, dump_graph, load_graph
from stateous.model.workflow import DefinitionError

from ...parallel import Review, review


class Approval(BaseModel):
    by: str


class Ticket(BaseModel):
    repo: str
    branch: str = "main"
    approval: Approval | None = None
    reviews: Branches | None = None
    note: str = ""


def approved_flow() -> Workflow:
    flow = Workflow("sdlc", context=Ticket)
    waiting = flow.state("waiting")
    approved = flow.event("approved", payload=Approval, into="approval")
    flow.transition(START, waiting)
    flow.transition(waiting, END, on=approved)
    return flow


def reviews_flow() -> Workflow:
    flow = Workflow("sdlc", context=Ticket)
    reviews = flow.state(
        "reviews", parallel=[review("security")], inputs=["repo", "branch"], into="reviews"
    )
    flow.transition(START, reviews)
    flow.transition(reviews, END, on="joined")
    return flow


# ── declaring it ────────────────────────────────────────────────────────


def test_an_event_names_the_field_that_keeps_its_payload() -> None:
    assert approved_flow().events["approved"].into == "approval"


def test_a_parallel_state_names_the_field_that_keeps_its_branches() -> None:
    assert reviews_flow().states["reviews"].into == "reviews"


def test_into_needs_a_payload() -> None:
    with pytest.raises(DefinitionError, match=r"^event 'done': into= needs a payload$"):
        Workflow("sdlc", context=Ticket).event("done", into="note")


def test_into_needs_a_context_model() -> None:
    with pytest.raises(
        DefinitionError,
        match=(
            r"^event 'approved': into= needs a context model; "
            r"give Workflow\(\.\.\., context=Model\)$"
        ),
    ):
        Workflow("sdlc").event("approved", payload=Approval, into="approval")


def test_into_names_a_field_of_the_context_model() -> None:
    with pytest.raises(
        DefinitionError,
        match=r"^event 'approved': into='nope' is not a field of the context model 'Ticket'$",
    ):
        Workflow("sdlc", context=Ticket).event("approved", payload=Approval, into="nope")


def test_the_field_must_take_the_payload_model() -> None:
    with pytest.raises(
        DefinitionError, match=r"^event 'approved': field 'note' is a str, not Approval$"
    ):
        Workflow("sdlc", context=Ticket).event("approved", payload=Approval, into="note")


def test_a_parallel_states_field_must_take_branches() -> None:
    flow = Workflow("sdlc", context=Ticket)
    with pytest.raises(
        DefinitionError, match=r"^state 'reviews': field 'note' is a str, not Branches$"
    ):
        flow.state("reviews", parallel=[review("security")], inputs=["repo", "branch"], into="note")


def test_into_on_a_state_is_for_a_parallel_state() -> None:
    with pytest.raises(
        DefinitionError,
        match=r"^state 'coding': into is for a parallel state; add parallel=\[\.\.\.\]$",
    ):
        Workflow("sdlc", context=Ticket).state("coding", into="reviews")


def test_a_field_with_no_none_takes_the_payload_model() -> None:
    class Strict(BaseModel):
        approval: Approval

    flow = Workflow("sdlc", context=Strict)

    assert flow.event("approved", payload=Approval, into="approval").into == "approval"


# ── the graph file ──────────────────────────────────────────────────────


def test_the_graph_file_has_into() -> None:
    assert approved_flow_document()["events"]["approved"]["into"] == "approval"
    assert compile_workflow(reviews_flow())["states"]["reviews"]["into"] == "reviews"


def test_a_graph_file_reads_back_with_into() -> None:
    document = approved_flow_document()

    assert load_graph(dump_graph(document)) == document


@pytest.mark.parametrize(
    ("where", "reason"),
    [
        (
            ("events", "approved"),
            "the graph file's event 'approved': 'into' is a context field, by name",
        ),
        (("states", "waiting"), "the graph file's state 'waiting': 'into' is for a parallel state"),
    ],
)
def test_a_bad_into_in_a_graph_file_is_refused(where: tuple[str, str], reason: str) -> None:
    document = copy.deepcopy(approved_flow_document())
    document[where[0]][where[1]]["into"] = 3 if where[0] == "events" else "approval"

    with pytest.raises(GraphFileError, match=reason):
        load_graph(dump_graph(document))


def test_a_child_review_without_into_still_compiles() -> None:
    child = Workflow("security", context=Review)
    done = child.state("done")
    child.transition(START, done)
    child.transition(done, END, on=child.event("ok"))

    assert "into" not in compile_workflow(child)["events"]["ok"]


# ── the runtime graph ───────────────────────────────────────────────────


def test_the_runtime_graph_names_the_field_for_each_state_and_event() -> None:
    events = Graph(approved_flow_document())
    joins = Graph(compile_workflow(reviews_flow()))

    assert events.into("waiting", "approved") == "approval"
    assert events.into("waiting", "timeout") is None
    assert joins.into("reviews", "joined") == "reviews"


def approved_flow_document() -> dict[str, Any]:
    return compile_workflow(approved_flow())
