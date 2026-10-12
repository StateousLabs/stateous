"""The control plane's view of the compiled workflow.

Made from the graph document, never from the Python definition. Guards are
parsed again from their text. Payloads and the context
are checked against the JSON Schemas in the document.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

from jsonschema import Draft202012Validator, ValidationError

from stateous.model.graph_file import checksum, structure
from stateous.model.guards import Guard, parse_guard

START = "__start__"
END = "__end__"
STARTED = "__started__"
"""The event on which a token leaves START. Only the engine fires it."""
TIMEOUT = "timeout"
ERROR = "error"
JOINED = "joined"
"""Fired on a token in a parallel state when the state's join is met."""
OWN_EVENTS = frozenset({STARTED, TIMEOUT, ERROR, JOINED})
"""Stateous's own events. Only the engine fires them. Transitions name `timeout`,
`error` and `joined`; no one outside may fire them."""


@dataclass(frozen=True)
class StateSpec:
    name: str
    emits: tuple[str, ...] = ()
    timeout: str | None = None
    retries: int = 0
    parallel: tuple[str, ...] = ()
    """A parallel state's child workflows, by name. Empty for a state with a task."""
    join: str = "all"
    inputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class TransitionSpec:
    source: str
    target: str
    event: str
    when: Guard | None = None
    otherwise: bool = False


class Graph:
    """One workflow of a compiled graph document, ready to route events."""

    def __init__(self, document: dict[str, Any], *, workflow: str | None = None) -> None:
        """The main workflow of `document`; with `workflow`, that child workflow."""
        body = document if workflow is None else document["workflows"][workflow]
        self.document = document
        """The whole graph document, as the graph file holds it."""
        self.name: str = workflow or document["workflow"]
        self.checksum: str = checksum(document)
        self.structure: str = structure(document)
        self._schemas: dict[str, Any] = document.get("schemas", {})
        self._context: str | None = body.get("context")
        self._events: dict[str, dict[str, Any]] = body["events"]
        self._states = {
            name: _state(name, spec)
            for name, spec in cast("dict[str, dict[str, Any]]", body["states"]).items()
        }
        self._transitions = [
            _transition(spec) for spec in cast("list[dict[str, Any]]", body["transitions"])
        ]

    @property
    def workflows(self) -> list[str]:
        """The child workflows of the document, by name: those that parallel states run."""
        return list(cast("dict[str, Any]", self.document.get("workflows", {})))

    def child(self, workflow: str) -> Graph:
        """The graph of the child workflow `workflow` of the same document."""
        return Graph(self.document, workflow=workflow)

    @property
    def starts(self) -> list[TransitionSpec]:
        """START's transitions, in declaration order."""
        return [t for t in self._transitions if t.source == START]

    def state(self, name: str) -> StateSpec:
        return self._states[name]

    def declares(self, event: str) -> bool:
        """Whether the workflow declares `event`. Stateous's own events are not declared."""
        return event in self._events

    def candidates(self, state: str, event: str) -> list[TransitionSpec]:
        """The transitions an event can take from a state, in declaration order."""
        return [t for t in self._transitions if t.source == state and t.event == event]

    def payload_errors(self, event: str, payload: dict[str, Any] | None) -> list[str]:
        schema = self._events.get(event, {}).get("payload")
        if schema is None:
            return [] if not payload else [f"{event!r} carries no payload"]
        return self._errors(schema, payload if payload is not None else {})

    def context_errors(self, context: dict[str, Any]) -> list[str]:
        return [] if self._context is None else self._errors(self._context, context)

    def with_defaults(self, context: dict[str, Any]) -> dict[str, Any]:
        """The context, with each field that its schema gives a default filled in."""
        if self._context is None:
            return dict(context)
        properties = cast(
            "dict[str, dict[str, Any]]", self._schemas[self._context].get("properties", {})
        )
        filled = {name: spec["default"] for name, spec in properties.items() if "default" in spec}
        return {**filled, **context}

    def _errors(self, schema: str, value: Any) -> list[str]:
        validator = Draft202012Validator({"$ref": f"#/schemas/{schema}", "schemas": self._schemas})
        # jsonschema's stubs leave one overload of iter_errors untyped.
        errors: Iterable[ValidationError] = validator.iter_errors(value)  # pyright: ignore[reportUnknownMemberType]
        return [
            f"{'.'.join(str(p) for p in error.absolute_path) or schema}: {error.message}"
            for error in errors
        ]


def _state(name: str, spec: dict[str, Any]) -> StateSpec:
    return StateSpec(
        name=name,
        emits=tuple(spec.get("emits", ())),
        timeout=spec.get("timeout"),
        retries=spec.get("retries", 0),
        parallel=tuple(spec.get("parallel", ())),
        join=spec.get("join", "all"),
        inputs=tuple(spec.get("inputs", ())),
    )


def _transition(spec: dict[str, Any]) -> TransitionSpec:
    when = spec.get("when")
    return TransitionSpec(
        source=spec["from"],
        target=spec["to"],
        event=spec.get("on", STARTED),
        when=parse_guard(when) if when is not None else None,
        otherwise=spec.get("otherwise", False),
    )
