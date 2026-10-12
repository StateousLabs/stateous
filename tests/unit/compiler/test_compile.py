"""A workflow -> the graph document (contract section 6)."""

from typing import Any, Generic, TypeVar

import pytest
from pydantic import BaseModel, create_model

from stateous import END, START, Workflow
from stateous.compiler.codegen import generate_contract
from stateous.compiler.compile import compile_workflow
from stateous.model.workflow import DefinitionError

from ...parallel import ALL_APPROVED, parallel_document

SDLC: dict[str, Any] = {
    "stateous": 1,
    "workflow": "sdlc",
    "context": "Ticket",
    "states": {
        "coding": {"timeout": "6h", "retries": 2, "emits": ["done", "stuck"]},
        "testing": {"timeout": "1h", "emits": ["passed", "failed"]},
    },
    "events": {
        "done": {},
        "stuck": {},
        "passed": {"payload": "Tests"},
        "failed": {"payload": "Tests"},
    },
    "transitions": [
        {"from": "__start__", "to": "coding"},
        {"from": "coding", "to": "testing", "on": "done"},
        {"from": "coding", "to": "__end__", "on": "stuck"},
        {"from": "testing", "to": "__end__", "on": "passed"},
        {"from": "testing", "to": "coding", "on": "failed", "when": "count('failed') < 3"},
        {"from": "testing", "to": "__end__", "on": "failed", "otherwise": True},
    ],
    "schemas": {
        "Tests": {
            "properties": {"passed": {"type": "integer"}, "failed": {"type": "integer"}},
            "required": ["passed", "failed"],
            "type": "object",
        },
        "Ticket": {
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
            "type": "object",
        },
    },
}


def test_the_designs_example_compiles_to_the_contracts_document(
    sdlc_document: dict[str, Any],
) -> None:
    assert sdlc_document == SDLC
    assert list(sdlc_document) == [
        "stateous",
        "workflow",
        "context",
        "states",
        "events",
        "transitions",
        "schemas",
    ]


def test_a_workflow_without_a_context_has_no_context_key() -> None:
    flow = Workflow("plain")
    a = flow.state("a")
    flow.transition(START, a)
    flow.transition(a, END, on=flow.event("done"))

    document = compile_workflow(flow)

    assert "context" not in document
    assert document["schemas"] == {}
    assert document["states"] == {"a": {"emits": ["done"]}}


def test_emits_leaves_out_stateous_own_events_and_follows_the_transitions() -> None:
    flow = Workflow("sdlc")
    waiting = flow.state("waiting", timeout="1d")
    review = flow.state("review", retries=1)
    flow.transition(START, waiting)
    flow.transition(waiting, END, on="timeout")
    approved, asked = flow.event("approved"), flow.event("asked")
    flow.transition(review, END, on=asked)
    flow.transition(review, END, on="error")
    flow.transition(review, END, on=approved, when="ctx['x'] == 1")
    flow.transition(review, review, on=approved, otherwise=True)
    flow.transition(waiting, review, on=asked)

    states = compile_workflow(flow)["states"]

    assert states["waiting"] == {"timeout": "1d", "emits": ["asked"]}
    assert states["review"] == {"retries": 1, "emits": ["asked", "approved"]}


def test_a_state_whose_only_way_out_is_stateous_own_emits_nothing() -> None:
    flow = Workflow("sdlc")
    waiting = flow.state("waiting", timeout="1h")
    flow.transition(START, waiting)
    flow.transition(waiting, END, on="timeout")

    assert compile_workflow(flow)["states"]["waiting"] == {"timeout": "1h", "emits": []}


class Failure(BaseModel):
    name: str
    title: str = ""  # a property called `title` is kept


class Report(BaseModel):
    failures: list[Failure] = []


def test_every_model_is_embedded_once_without_titles_and_refers_by_schemas() -> None:
    flow = Workflow("sdlc")
    a = flow.state("a")
    flow.transition(START, a)
    flow.transition(a, END, on=flow.event("passed", payload=Report))
    flow.transition(a, a, on=flow.event("failed", payload=Report))

    schemas = compile_workflow(flow)["schemas"]

    assert list(schemas) == ["Failure", "Report"]
    assert schemas["Report"]["properties"]["failures"]["items"] == {"$ref": "#/schemas/Failure"}
    assert "title" not in schemas["Failure"]
    assert schemas["Failure"]["properties"]["title"] == {"default": "", "type": "string"}


T = TypeVar("T")


class Box(BaseModel, Generic[T]):
    item: T


def boxed(context: type[BaseModel], payload: type[BaseModel]) -> Workflow:
    flow = Workflow("boxes", context=context)
    a = flow.state("a")
    flow.transition(START, a)
    flow.transition(a, END, on=flow.event("done", payload=payload))
    return flow


def test_a_generic_model_is_named_by_its_key_in_schemas() -> None:
    document = compile_workflow(boxed(Box[int], Box[str]))

    assert document["context"] == "Box_int_"
    assert document["events"]["done"] == {"payload": "Box_str_"}
    assert sorted(document["schemas"]) == ["Box_int_", "Box_str_"]
    source = generate_contract(document)
    assert "Box_int_Context = Context[Box_int_]" in source
    compile(source, "contract.py", "exec")  # it is valid Python


def test_a_model_name_that_is_not_a_python_name_is_refused() -> None:
    odd = create_model("odd-name", repo=(str, ...))

    with pytest.raises(DefinitionError, match="the model name 'odd-name' cannot be a Python name"):
        compile_workflow(boxed(odd, Box[str]))


# ── parallel states ────────────────────────────────────


def test_a_parallel_state_names_its_workflows_join_and_inputs_in_place_of_emits() -> None:
    document = parallel_document("any")

    assert document["states"]["reviews"] == {
        "timeout": "2d",
        "parallel": ["security", "design"],
        "join": "any",
        "inputs": ["repo", "branch"],
    }
    assert document["states"]["coding"] == {"emits": ["done"]}
    assert {"from": "reviews", "to": "merging", "on": "joined", "when": ALL_APPROVED} in (
        document["transitions"]
    )


def test_the_child_workflows_are_compiled_inside_the_document() -> None:
    document = parallel_document()

    assert list(document) == [
        "stateous", "workflow", "context", "states", "events", "transitions", "workflows",
        "schemas",
    ]  # fmt: skip
    assert list(document["workflows"]) == ["security", "design"]
    assert document["workflows"]["security"] == {
        "context": "Review",
        "states": {
            "triage": {"emits": ["lgtm", "deny"]},
            "approved": {"emits": ["recorded"]},
            "rejected": {"emits": ["recorded"]},
        },
        "events": {"recorded": {}, "lgtm": {}, "deny": {"payload": "Findings"}},
        "transitions": [
            {"from": "__start__", "to": "triage"},
            {"from": "triage", "to": "approved", "on": "lgtm"},
            {"from": "triage", "to": "rejected", "on": "deny"},
            {"from": "approved", "to": "__end__", "on": "recorded"},
            {"from": "rejected", "to": "__end__", "on": "recorded"},
        ],
    }
    assert sorted(document["schemas"]) == ["Findings", "Review", "Ticket"]


def test_a_workflow_without_parallel_states_has_no_workflows_key(
    sdlc_document: dict[str, Any],
) -> None:
    assert "workflows" not in sdlc_document
