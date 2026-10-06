"""A workflow -> the graph document (contract section 6)."""

from typing import Any

from pydantic import BaseModel

from stateous import END, START, Workflow
from stateous.compiler.compile import compile_workflow

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
