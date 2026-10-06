"""The types that a generated `contract.py` is built from.

A supervisor never sees the workflow. It sees a generated contract: one
`StateRef` per state, each naming the events that state may emit. Nothing here
is written by hand in user code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

from pydantic import ValidationError

from stateous.model.events import Occurrence

F = TypeVar("F", bound=Callable[..., Any])
O = TypeVar("O", bound=Occurrence[Any])  # noqa: E741 — contract section 18, R4, names it O
"""The events of one state: the union of its occurrence classes."""
_LABEL = "__stateous_implements__"
_DEFS = "#/$defs/"


class OutcomeError(ValueError):
    """An outcome does not fit its state: an event the state may not emit, or a
    payload that does not fit the event's model."""


@dataclass(frozen=True)
class StateRef(Generic[O]):
    """A state as a supervisor sees it. The generated `contract.py` has one per
    state, as `states.<name>`, for `@on_state`.

    The type argument is the union of the state's events. Pyright gets it from
    `emits`: `StateRef("coding", emits=(done, stuck))` is a `StateRef[done | stuck]`,
    so `outcome()` gives a `done | stuck`."""

    name: str
    """The state's name."""
    emits: tuple[type[O], ...] = ()
    """The events the state may be left on: those it has transitions for."""

    def may_emit(self, occurrence: Occurrence[Any]) -> bool:
        """Whether the state may be left on `occurrence`."""
        return any(type(occurrence) is event for event in self.emits)

    def outcome_schema(self) -> dict[str, Any]:
        """The events this state may emit, with their payloads, as one JSON schema.
        An agent's final answer that fits it names one event and its payload."""
        return {"oneOf": [_branch(event) for event in self.emits]}

    def outcome(self, data: Any) -> O:
        """The occurrence that `data` names: `{"event": ..., "payload": ...}`.
        `OutcomeError` when `data` does not fit `outcome_schema()`."""
        allowed = self._allowed()
        if not isinstance(data, dict):
            raise OutcomeError(
                f"an outcome of {self.name!r} is an object with 'event' and 'payload'; {allowed}"
            )
        body = cast("dict[str, Any]", data)
        extra = sorted(set(body) - {"event", "payload"})
        if extra:
            raise OutcomeError(
                f"an outcome of {self.name!r} has only 'event' and 'payload', "
                f"not {extra[0]!r}; {allowed}"
            )
        name = body.get("event")
        event = next((e for e in self.emits if e.name == name), None)
        if event is None:
            raise OutcomeError(f"{self.name!r} may not emit {name!r}; {allowed}")
        model = event.payload_model()
        payload = body.get("payload")
        if model is None:
            if payload is not None:
                raise OutcomeError(
                    f"{name!r} carries no payload, but the outcome has one; {allowed}"
                )
            return event()
        try:
            return event(model.model_validate(payload))
        except ValidationError as error:
            first = error.errors()[0]
            where = ".".join(str(part) for part in first["loc"]) or "payload"
            raise OutcomeError(
                f"the payload of {name!r} does not fit {model.__name__} "
                f"({where}: {first['msg']}); {allowed}"
            ) from None

    def _allowed(self) -> str:
        names = ", ".join(repr(event.name) for event in self.emits)
        return f"{self.name!r} may emit {names}" if names else f"{self.name!r} may emit no event"


def _branch(event: type[Occurrence[Any]]) -> dict[str, Any]:
    properties: dict[str, Any] = {"event": {"const": event.name}}
    required = ["event"]
    model = event.payload_model()
    if model is not None:
        properties["payload"] = _inlined(model.model_json_schema())
        required.append("payload")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _inlined(schema: dict[str, Any]) -> dict[str, Any]:
    """`schema` with each `#/$defs/...` reference replaced by its definition, so
    the branch stands alone inside the outcome schema."""
    definitions = cast("dict[str, Any]", schema.get("$defs", {}))
    body = {key: value for key, value in schema.items() if key != "$defs"}
    return cast("dict[str, Any]", _resolve(body, definitions, ()))


def _resolve(node: Any, definitions: dict[str, Any], path: tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_resolve(item, definitions, path) for item in cast("list[Any]", node)]
    if not isinstance(node, dict):
        return node
    mapping = cast("dict[str, Any]", node)
    ref = mapping.get("$ref")
    if isinstance(ref, str) and ref.startswith(_DEFS):
        name = ref.removeprefix(_DEFS)
        if name in path:
            raise OutcomeError(
                f"the payload model {name} refers to itself; an outcome schema cannot hold it"
            )
        rest = {key: value for key, value in mapping.items() if key != "$ref"}
        merged = {**cast("dict[str, Any]", definitions[name]), **rest}
        return _resolve(merged, definitions, (*path, name))
    return {key: _resolve(value, definitions, path) for key, value in mapping.items()}


# ── Labelling routines ─────────────────────────────────────────────────────
# The decorator only attaches a label. Nothing is registered at import time: the
# supervisor finds labelled functions in the files of its folder.


def on_state(state: StateRef[Any]) -> Callable[[F], F]:
    """Mark a function as the routine for `state`: it runs when the state has a task."""
    if not isinstance(state, StateRef):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f"@on_state takes a state from contract.states, not {state!r}")

    def decorate(function: F) -> F:
        setattr(function, _LABEL, state)
        return function

    return decorate


def implementation_of(function: Callable[..., Any]) -> StateRef[Any] | None:
    """The state a function is the routine for, if any."""
    label: object = getattr(function, _LABEL, None)
    return cast("StateRef[Any]", label) if isinstance(label, StateRef) else None
