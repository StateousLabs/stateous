"""The graph document -> a supervisor's `contract.py`, and a stub routine per state.

Generated from the document, never from Python objects: `stateous init` has only
the graph that the control plane serves. The output follows ruff's format for
lines of up to 100 characters.
"""

from __future__ import annotations

import json
import keyword
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

from stateous.model.graph_file import checksum

_SIMPLE = {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "null": "None"}
_DASH = "—"
_TAKEN = {
    "Any",
    "BaseModel",
    "Context",
    "Literal",
    "Never",
    "Occurrence",
    "StateRef",
    "events",
    "states",
}
"""Names the contract uses itself. No model may take one."""


class CodegenError(ValueError):
    """The document cannot be written as Python."""


@dataclass(frozen=True)
class _Field:
    name: str
    annotation: str
    default: str | None
    """Python source of the default, or None when the field is required."""


@dataclass(frozen=True)
class _Model:
    name: str
    fields: list[_Field]


def generate_contract(document: dict[str, Any]) -> str:
    """The text of `contract.py`: the models, `events`, `states` (contract section 7)."""
    workflow = cast("str", document["workflow"])
    schemas = cast("dict[str, dict[str, Any]]", document.get("schemas", {}))
    events = cast("dict[str, dict[str, Any]]", document["events"])
    states = cast("dict[str, dict[str, Any]]", document["states"])
    context = cast("str | None", document.get("context"))
    _unique("event", events)
    _unique("state", states)
    models = [_model(name, schemas[name]) for name in _ordered(schemas)]
    _model_names([model.name for model in models], context)
    seal = checksum(document)

    annotations = " ".join(f.annotation for model in models for f in model.fields)
    typing = [name for name in ("Any", "Literal") if re.search(rf"\b{name}\b", annotations)]
    if any(not spec.get("emits") for spec in states.values()):
        typing.append("Never")  # for `StateRef[Never]`, a state with no events
    used = [("Context", context is not None), ("Occurrence", bool(events)), ("StateRef", True)]
    imported = [name for name, needed in used if needed]

    lines = [
        f"# GENERATED from the `{workflow}` workflow {_DASH} do not edit.",
        f"# {seal}",
        "from __future__ import annotations",
        "",
    ]
    if typing:
        lines += [f"from typing import {', '.join(typing)}", ""]
    if models:
        lines += ["from pydantic import BaseModel", ""]
    lines += [
        f"from stateous import {', '.join(imported)}",
        "",
        f"__workflow__ = {json.dumps(workflow)}",
        f"__checksum__ = {json.dumps(seal)}",
    ]
    for model in models:
        lines += ["", "", f"class {model.name}(BaseModel):"]
        if not model.fields:
            lines.append("    pass")
        for f in model.fields:
            default = "" if f.default is None else f" = {f.default}"
            lines.append(f"    {f.name}: {f.annotation}{default}")
    if context is not None:
        lines += ["", "", f"{context}Context = Context[{context}]"]
    lines += ["", "", "class events:"]
    if not events:
        lines.append("    pass")
    for i, (name, spec) in enumerate(events.items()):
        if i:
            lines.append("")
        payload = spec.get("payload", "None")
        lines += [
            f"    class {_identifier(name)}(Occurrence[{payload}]):",
            f"        name = {json.dumps(name)}",
        ]
    lines += ["", "", "class states:"]
    if not states:
        lines.append("    pass")
    for name, spec in states.items():
        emits = [f"events.{_identifier(event)}" for event in spec.get("emits", [])]
        listed = "(" + ", ".join(emits) + ("," if len(emits) == 1 else "") + ")"
        # Pyright gets `StateRef[events.a | events.b]` from `emits`. An empty tuple
        # gives it nothing, so a state with no events says `Never` itself.
        ref = "StateRef" if emits else "StateRef[Never]"
        lines.append(f"    {_identifier(name)} = {ref}({json.dumps(name)}, emits={listed})")
    return "\n".join(lines) + "\n"


def generate_stub(document: dict[str, Any], state: str) -> str:
    """A routine file for one state: typed, labelled, and not yet doing anything."""
    states = cast("dict[str, dict[str, Any]]", document["states"])
    spec = states.get(state)
    if spec is None:
        raise CodegenError(f"the workflow {document['workflow']!r} has no state {state!r}")
    function = _identifier(state)
    outcomes = [f"events.{_identifier(event)}" for event in spec.get("emits", [])]
    context = cast("str | None", document.get("context"))
    ctx_type = f"{context}Context" if context is not None else "Context"
    from_stateous = "on_state" if context is not None else "Context, on_state"
    from_contract = [
        *([ctx_type] if context is not None else []),
        *(["events"] if outcomes else []),
        "states",
    ]
    message = json.dumps(f"do the work of {state!r} here")
    lines = [
        f'"""The `{state}` state of the `{document["workflow"]}` workflow."""',
        "",
        f"from stateous import {from_stateous}",
        "",
        f"from contract import {', '.join(from_contract)}",
        "",
        "",
        f"@on_state(states.{function})",
        f"def {function}(ctx: {ctx_type}) -> {' | '.join(outcomes) or 'None'}:",
        f"    raise NotImplementedError({message})",
    ]
    return "\n".join(lines) + "\n"


def _unique(what: str, names: Iterable[str]) -> None:
    """Each name must become its own Python name."""
    seen: dict[str, str] = {}
    for name in names:
        identifier = _identifier(name)
        if identifier in seen:
            raise CodegenError(
                f"the {what}s {seen[identifier]!r} and {name!r} are both {identifier!r} "
                "in Python; rename one"
            )
        seen[identifier] = name


def _model_names(names: list[str], context: str | None) -> None:
    for name in names:
        if name in _TAKEN:
            raise CodegenError(
                f"a model is named {name!r}, which contract.py uses itself; rename the model"
            )
    if context is not None and f"{context}Context" in names:
        raise CodegenError(
            f"a model is named {context + 'Context'!r}, which contract.py uses for the "
            f"context of {context}; rename the model"
        )


def _model(name: str, schema: dict[str, Any]) -> _Model:
    properties = cast("dict[str, dict[str, Any]]", schema.get("properties", {}))
    required = set(cast("list[str]", schema.get("required", [])))
    fields: list[_Field] = []
    for field_name, field_schema in properties.items():
        if not field_name.isidentifier() or keyword.iskeyword(field_name):
            raise CodegenError(
                f"the field {field_name!r} of {name} cannot be used as a Python name"
            )
        default = None if field_name in required else _literal(field_schema.get("default"))
        fields.append(_Field(field_name, _annotation(field_schema), default))
    return _Model(name, fields)


def _annotation(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    if "anyOf" in schema:
        options = cast("list[dict[str, Any]]", schema["anyOf"])
        return " | ".join(_annotation(option) for option in options)
    if "enum" in schema:
        return "Literal[" + ", ".join(_literal(v) for v in cast("list[Any]", schema["enum"])) + "]"
    if "const" in schema:
        return f"Literal[{_literal(schema['const'])}]"
    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(_annotation({**schema, "type": k}) for k in cast("list[str]", kind))
    if kind == "array":
        items = schema.get("items")
        return f"list[{_annotation(cast('dict[str, Any]', items))}]" if items else "list[Any]"
    if kind == "object":
        values = schema.get("additionalProperties")
        if isinstance(values, dict):
            return f"dict[str, {_annotation(cast('dict[str, Any]', values))}]"
        return "dict[str, Any]"
    if isinstance(kind, str) and kind in _SIMPLE:
        return _SIMPLE[kind]
    return "Any"


def _literal(value: Any) -> str:
    """A JSON value as Python source, with double-quoted strings."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_literal(item) for item in cast("list[Any]", value)) + "]"
    if isinstance(value, dict):
        pairs = cast("dict[str, Any]", value).items()
        return "{" + ", ".join(f"{_literal(k)}: {_literal(v)}" for k, v in pairs) + "}"
    return repr(value)  # numbers, True, False, None


def _ordered(schemas: dict[str, dict[str, Any]]) -> list[str]:
    """The models, each after the models it refers to; otherwise by name."""
    ordered: list[str] = []

    def visit(name: str, path: tuple[str, ...]) -> None:
        if name in ordered:
            return
        if name in path:
            raise CodegenError(f"the model {name!r} refers to itself; that is not supported")
        for dependency in sorted(_references(schemas[name])):
            if dependency in schemas:
                visit(dependency, (*path, name))
        ordered.append(name)

    for name in sorted(schemas):
        visit(name, ())
    return ordered


def _references(schema: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(schema, dict):
        for key, value in cast("dict[str, Any]", schema).items():
            if key == "$ref":
                found.add(str(value).rsplit("/", 1)[-1])
            else:
                found |= _references(value)
    elif isinstance(schema, list):
        for item in cast("list[Any]", schema):
            found |= _references(item)
    return found


def _identifier(name: str) -> str:
    identifier = name.replace("-", "_")
    if not identifier.isidentifier() or keyword.iskeyword(identifier):
        raise CodegenError(f"{name!r} cannot be used as a Python name in the generated contract")
    return identifier
