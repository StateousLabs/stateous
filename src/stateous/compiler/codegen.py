"""The graph document -> a supervisor's `contract.py`, and a stub routine per state.

Generated from the document, never from Python objects: `stateous init supervisor`
can have only the graph that the control plane serves. The output follows ruff's format for
lines of up to 100 characters.

Each child workflow of a parallel state is a class in `events` and in `states` (design
section 8). A parallel state has no routine, so it has no `StateRef` and no stub.
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
    "Field",
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
    alias: str | None = None
    """For an Enum or a constant: the `Literal[...]` it is written as, not a class."""


Body = tuple[str | None, dict[str, Any]]
"""A workflow of the document: None for the main one, else the child's name; and its
body (`context`, `states`, `events`, `transitions`)."""


def generate_contract(document: dict[str, Any]) -> str:
    """The text of `contract.py`: the models, `events`, `states`."""
    workflow = cast("str", document["workflow"])
    schemas = cast("dict[str, dict[str, Any]]", document.get("schemas", {}))
    bodies = _bodies(document)
    for _, body in bodies:
        _unique("event", _events_of(body))
        _unique("state", _states_of(body))
    _namespaces(document)
    _known_models(bodies, schemas)
    models = [_model(name, schemas[name]) for name in _ordered(schemas)]
    contexts = list(
        dict.fromkeys(str(body["context"]) for _, body in bodies if body.get("context"))
    )
    _model_names([model.name for model in models], contexts)
    seal = checksum(document)

    annotations = " ".join(
        [model.alias or "" for model in models]
        + [f.annotation for model in models for f in model.fields]
    )
    typing = [name for name in ("Any", "Literal") if re.search(rf"\b{name}\b", annotations)]
    states = [spec for _, body in bodies for spec in _states_of(body).values()]
    if any(not spec.get("emits") and "parallel" not in spec for spec in states):
        typing.append("Never")  # for `StateRef[Never]`, a state with no events
    has_events = any(_events_of(body) for _, body in bodies)
    used = [("Context", bool(contexts)), ("Occurrence", has_events), ("StateRef", True)]
    imported = [name for name, needed in used if needed]

    lines = [
        f"# GENERATED from the `{workflow}` workflow {_DASH} do not edit.",
        f"# {seal}",
        "from __future__ import annotations",
        "",
    ]
    if typing:
        lines += [f"from typing import {', '.join(typing)}", ""]
    classes = [model for model in models if model.alias is None]
    if classes:
        defaults = [f.default or "" for model in classes for f in model.fields]
        field = ", Field" if any(d.startswith("Field(") for d in defaults) else ""
        lines += [f"from pydantic import BaseModel{field}", ""]
    lines += [
        f"from stateous import {', '.join(imported)}",
        "",
        f"__workflow__ = {json.dumps(workflow)}",
        f"__checksum__ = {json.dumps(seal)}",
    ]
    for model in models:
        if model.alias is not None:
            lines += ["", "", f"{model.name} = {model.alias}"]
            continue
        lines += ["", "", f"class {model.name}(BaseModel):"]
        if not model.fields:
            lines.append("    pass")
        for f in model.fields:
            default = "" if f.default is None else f" = {f.default}"
            lines.append(f"    {f.name}: {f.annotation}{default}")
    if contexts:
        lines += ["", "", *(f"{context}Context = Context[{context}]" for context in contexts)]
    lines += ["", "", "class events:", *_event_lines(bodies)]
    lines += ["", "", "class states:", *_state_lines(bodies)]
    return "\n".join(lines) + "\n"


def generate_stub(document: dict[str, Any], state: str) -> str:
    """A routine file for one state: typed, labelled, and not yet doing anything. A
    state of a child workflow is named `<workflow>/<state>`, as a claim names it."""
    workflow, _, own = state.rpartition("/")
    children = cast("dict[str, dict[str, Any]]", document.get("workflows", {}))
    body = children.get(workflow) if workflow else document
    spec = None if body is None else _states_of(body).get(own)
    if body is None or spec is None:
        raise CodegenError(f"the workflow {document['workflow']!r} has no state {state!r}")
    if "parallel" in spec:
        raise CodegenError(
            f"{state!r} is a parallel state: its work is its child workflows, so it has no routine"
        )
    namespace = f"{_identifier(workflow)}." if workflow else ""
    function = _identifier(own)
    outcomes = [f"events.{namespace}{_identifier(event)}" for event in spec.get("emits", [])]
    context = cast("str | None", body.get("context"))
    ctx_type = f"{context}Context" if context is not None else "Context"
    from_stateous = "on_state" if context is not None else "Context, on_state"
    from_contract = [
        *([ctx_type] if context is not None else []),
        *(["events"] if outcomes else []),
        "states",
    ]
    message = json.dumps(f"do the work of {state!r} here")
    lines = [
        f'"""The `{own}` state of the `{workflow or document["workflow"]}` workflow."""',
        "",
        f"from stateous import {from_stateous}",
        "",
        f"from contract import {', '.join(from_contract)}",
        "",
        "",
        f"@on_state(states.{namespace}{function})",
        f"def {function}(ctx: {ctx_type}) -> {' | '.join(outcomes) or 'None'}:",
        f"    raise NotImplementedError({message})",
    ]
    return "\n".join(lines) + "\n"


def routine_states(document: dict[str, Any]) -> list[str]:
    """The states that have a routine, as a supervisor names them in a claim: the main
    workflow's as `<state>`, then each child workflow's as `<workflow>/<state>`. A
    parallel state has no routine."""
    found = [name for name, spec in _states_of(document).items() if "parallel" not in spec]
    children = cast("dict[str, dict[str, Any]]", document.get("workflows", {}))
    for workflow, body in children.items():
        found += [
            f"{workflow}/{name}"
            for name, spec in _states_of(body).items()
            if "parallel" not in spec
        ]
    return found


def _bodies(document: dict[str, Any]) -> list[Body]:
    """The main workflow (None) and each child workflow (its name), with its body."""
    children = cast("dict[str, dict[str, Any]]", document.get("workflows", {}))
    found: list[Body] = [(None, document)]
    found += list(children.items())
    return found


def _states_of(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return cast("dict[str, dict[str, Any]]", body["states"])


def _events_of(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return cast("dict[str, dict[str, Any]]", body["events"])


def _namespaces(document: dict[str, Any]) -> None:
    """Each child workflow is a class in `events` and in `states`. Its Python name may
    not be the name of another child, or of an event or a state of the main workflow."""
    children = list(cast("dict[str, Any]", document.get("workflows", {})))
    _unique("child workflow", children)
    main = [*_events_of(document)]
    main += [name for name, spec in _states_of(document).items() if "parallel" not in spec]
    taken = {_identifier(name): name for name in main}
    for child in children:
        identifier = _identifier(child)
        if identifier in taken:
            raise CodegenError(
                f"the child workflow {child!r} and {taken[identifier]!r} of the workflow "
                f"{document['workflow']!r} are both {identifier!r} in Python; rename one"
            )


def _event_lines(bodies: list[Body]) -> list[str]:
    """The body of `class events:`: the main workflow's events, then a class for each
    child workflow that has events. Blank lines as ruff puts them."""
    blocks: list[list[str]] = []
    for prefix, body in bodies:
        pad = "    " if prefix is None else "        "
        classes = [
            [
                f"{pad}class {_identifier(name)}(Occurrence[{spec.get('payload', 'None')}]):",
                f"{pad}    name = {json.dumps(name)}",
            ]
            for name, spec in _events_of(body).items()
        ]
        if prefix is None:
            blocks += classes
        elif classes:
            blocks.append([f"    class {_identifier(prefix)}:", *_joined(classes)])
    return _joined(blocks) or ["    pass"]


def _state_lines(bodies: list[Body]) -> list[str]:
    """The body of `class states:`: a `StateRef` for each state with a routine, the
    main workflow's first, then a class for each child workflow. A child's state is
    named `<workflow>/<state>`, as a claim names it."""
    main: list[str] = []
    blocks: list[list[str]] = []
    for prefix, body in bodies:
        pad = "    " if prefix is None else "        "
        namespace = "events." if prefix is None else f"events.{_identifier(prefix)}."
        refs: list[str] = []
        for name, spec in _states_of(body).items():
            if "parallel" in spec:
                continue  # its work is its child workflows: no routine
            emits = [f"{namespace}{_identifier(event)}" for event in spec.get("emits", [])]
            listed = "(" + ", ".join(emits) + ("," if len(emits) == 1 else "") + ")"
            # Pyright gets `StateRef[events.a | events.b]` from `emits`. An empty tuple
            # gives it nothing, so a state with no events says `Never` itself.
            ref = "StateRef" if emits else "StateRef[Never]"
            qualified = json.dumps(name if prefix is None else f"{prefix}/{name}")
            refs.append(f"{pad}{_identifier(name)} = {ref}({qualified}, emits={listed})")
        if prefix is None:
            main = refs
        elif refs:
            blocks.append([f"    class {_identifier(prefix)}:", *refs])
    lines = list(main)
    for block in blocks:
        if lines:
            lines.append("")
        lines += block
    return lines or ["    pass"]


def _joined(blocks: list[list[str]]) -> list[str]:
    """The blocks, with one blank line between each two."""
    lines: list[str] = []
    for block in blocks:
        if lines:
            lines.append("")
        lines += block
    return lines


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


def _known_models(bodies: list[Body], schemas: dict[str, Any]) -> None:
    """Each model the document names, as a context or a payload, is a key in `schemas`."""
    for prefix, body in bodies:
        where = "" if prefix is None else f" of the child workflow {prefix!r}"
        used = [(f"the context{where}", body.get("context"))]
        used += [
            (f"the event {name!r}{where}", spec.get("payload"))
            for name, spec in _events_of(body).items()
        ]
        for what, name in used:
            if name is not None and name not in schemas:
                raise CodegenError(
                    f"{what} names the model {name!r}, which 'schemas' does not have"
                )


def _model_names(names: list[str], contexts: list[str]) -> None:
    for name in names:
        if name in _TAKEN:
            raise CodegenError(
                f"a model is named {name!r}, which contract.py uses itself; rename the model"
            )
    for context in contexts:
        if f"{context}Context" in names:
            raise CodegenError(
                f"a model is named {context + 'Context'!r}, which contract.py uses for the "
                f"context of {context}; rename the model"
            )


def _model(name: str, schema: dict[str, Any]) -> _Model:
    if "enum" in schema or "const" in schema:  # pydantic writes an Enum this way
        return _Model(name, [], alias=_annotation(schema))
    properties = cast("dict[str, dict[str, Any]]", schema.get("properties", {}))
    required = set(cast("list[str]", schema.get("required", [])))
    fields: list[_Field] = []
    for field_name, field_schema in properties.items():
        if not field_name.isidentifier() or keyword.iskeyword(field_name):
            raise CodegenError(
                f"the field {field_name!r} of {name} cannot be used as a Python name"
            )
        annotation = _annotation(field_schema)
        default = None if field_name in required else _default(field_schema, annotation)
        fields.append(_Field(field_name, annotation, default))
    return _Model(name, fields)


def _default(schema: dict[str, Any], annotation: str) -> str | None:
    """The default of a field that is not required. Pydantic writes no `default`
    for a `default_factory`: an empty list or dict is made again here, by its full
    type (`list[Sev]`, not `list`), so that strict pyright knows the type of its
    items. Any other field without a default becomes required."""
    if "default" in schema:
        return _literal(schema["default"])
    if schema.get("type") not in ("array", "object"):
        return None
    return f"Field(default_factory={annotation})"


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
