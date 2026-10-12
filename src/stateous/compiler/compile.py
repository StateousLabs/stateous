"""A workflow -> the graph document: plain data, written as the graph file.

The document holds all that the control plane and `stateous init` need: each
state's settings and the events it may emit, the events, the transitions, and
each model as JSON Schema. The child workflows of parallel states are in it too,
under `workflows`. Nothing in it refers back to Python code.
"""

from __future__ import annotations

import keyword
from typing import Any, cast

from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from stateous.compiler.validate import nested_workflows, validate
from stateous.model.graph_file import FORMAT
from stateous.model.workflow import (
    RESERVED_EVENTS,
    START,
    DefinitionError,
    State,
    Transition,
    Workflow,
)

SCHEMA_REF = "#/schemas/{model}"


def compile_workflow(flow: Workflow) -> dict[str, Any]:
    """Validate `flow` (`DefinitionError` lists every problem), then build its document.
    The child workflows of its parallel states go under `workflows` (see the guide,
    "Nested and parallel work")."""
    validate(flow)
    children = nested_workflows(flow)
    schemas, names = _schemas([flow, *children])
    _refuse_odd_names(flow, schemas)
    document: dict[str, Any] = {"stateous": FORMAT, "workflow": flow.name, **_body(flow, names)}
    if children:
        document["workflows"] = {child.name: _body(child, names) for child in children}
    document["schemas"] = schemas
    return document


def _body(flow: Workflow, names: dict[type[BaseModel], str]) -> dict[str, Any]:
    """One workflow: its context, states, events and transitions."""
    body: dict[str, Any] = {}
    if flow.context is not None:
        body["context"] = names[flow.context]
    body["states"] = {state.name: _state(flow, state) for state in flow.states.values()}
    body["events"] = {
        event.name: {"payload": names[event.payload]} if event.payload else {}
        for event in flow.events.values()
    }
    body["transitions"] = [_transition(edge) for edge in flow.transitions]
    return body


def _state(flow: Workflow, state: State) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if state.timeout is not None:
        out["timeout"] = state.timeout
    if state.retries:
        out["retries"] = state.retries
    if state.parallel:  # its work is its children: no task, so no events to emit
        out["parallel"] = [child.name for child in state.parallel]
        out["join"] = state.join
        if state.inputs:
            out["inputs"] = list(state.inputs)
    else:
        out["emits"] = _emits(flow, state)
    return out


def _emits(flow: Workflow, state: State) -> list[str]:
    """The declared events this state has a way out on, in the order of the transitions."""
    emitted: list[str] = []
    for edge in flow.transitions:
        name = edge.event.name
        if edge.source is state and name not in RESERVED_EVENTS and name not in emitted:
            emitted.append(name)
    return emitted


def _transition(edge: Transition) -> dict[str, Any]:
    out: dict[str, Any] = {"from": edge.source.name, "to": edge.target.name}
    if edge.source is not START:  # the engine takes START's transitions on __started__
        out["on"] = edge.event.name
    if edge.when is not None:
        out["when"] = edge.when.expression
    if edge.otherwise:
        out["otherwise"] = True
    return out


def _schemas(flows: list[Workflow]) -> tuple[dict[str, Any], dict[type[BaseModel], str]]:
    """Each model's JSON Schema, by its key; and the key of each model the workflows
    name. Pydantic chooses the keys: `Box[int]` is `Box_int_`."""
    models: list[type[BaseModel]] = []
    for flow in flows:
        for model in [flow.context, *(event.payload for event in flow.events.values())]:
            if model is not None and model not in models:
                models.append(model)
    if not models:
        return {}, {}
    refs, top = models_json_schema(
        [(model, "validation") for model in models], ref_template=SCHEMA_REF
    )
    prefix = SCHEMA_REF.removesuffix("{model}")
    names = {m: str(refs[(m, "validation")]["$ref"]).removeprefix(prefix) for m in models}
    definitions = cast("dict[str, Any]", top.get("$defs", {}))
    schemas = {name: _without_titles(definitions[name]) for name in sorted(definitions)}
    return schemas, names


def _refuse_odd_names(flow: Workflow, schemas: dict[str, Any]) -> None:
    """contract.py names a class after each key in `schemas`."""
    for name in schemas:
        if not name.isidentifier() or keyword.iskeyword(name):
            raise DefinitionError(
                f"workflow {flow.name!r} is not valid:\n  - the model name {name!r} cannot "
                "be a Python name in contract.py; rename the model"
            )


def _without_titles(schema: Any) -> Any:
    """Drop pydantic's generated `title`s; the names already say it. Only keys of
    schema objects are dropped: a property called `title` is kept."""
    if not isinstance(schema, dict):
        return schema
    cleaned: dict[str, Any] = {}
    for key, value in cast("dict[str, Any]", schema).items():
        if key == "title":
            continue
        if key == "properties":
            properties = cast("dict[str, Any]", value)
            cleaned[key] = {name: _without_titles(sub) for name, sub in properties.items()}
        elif key in ("items", "additionalProperties"):
            cleaned[key] = _without_titles(value)
        elif key in ("anyOf", "allOf", "oneOf"):
            cleaned[key] = [_without_titles(sub) for sub in cast("list[Any]", value)]
        else:
            cleaned[key] = value
    return cleaned
