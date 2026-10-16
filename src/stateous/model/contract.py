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
O = TypeVar("O", bound=Occurrence[Any])  # noqa: E741 — the convention for this parameter is O
"""The events of one state: the union of its occurrence classes."""
_LABEL = "__stateous_implements__"
_DEFS = "#/$defs/"


class OutcomeError(ValueError):
    """An outcome does not fit its state. The state cannot emit the event, or the
    payload does not agree with the event's model. `StateRef.outcome()` raises it."""


@dataclass(frozen=True)
class StateRef(Generic[O]):
    """A state as a supervisor sees it. The generated `contract.py` has one for
    each state, as `states.<name>`. Give it to `@on_state`.

    You do not write a `StateRef`: `stateous init supervisor` writes them. The
    type argument is the union of the state's events. For example,
    `StateRef("coding", emits=(done, stuck))` is a `StateRef[done | stuck]`. So
    `outcome()` returns a `done | stuck`."""

    name: str
    """The state's name."""
    emits: tuple[type[O], ...] = ()
    """The events that the state can emit: the events of its transitions."""

    def may_emit(self, occurrence: Occurrence[Any]) -> bool:
        """Tell if the state can emit an occurrence.

        Args:
            occurrence: An occurrence of an event.

        Returns:
            True when the event of `occurrence` is one of `emits`.
        """
        return any(type(occurrence) is event for event in self.emits)

    def outcome_schema(self) -> dict[str, Any]:
        """The events that this state can emit, with their payloads, as one JSON
        Schema. Give it to an agent as the form of its final answer.

        Returns:
            A JSON Schema with one branch for each event. An answer that agrees
                with it names one event and its payload.

        Raises:
            OutcomeError: The state can emit no event, so no answer agrees.
        """
        if not self.emits:
            raise OutcomeError(f"{self._allowed()}, so no outcome schema fits it")
        return {"oneOf": [_branch(event) for event in self.emits]}

    def outcome(self, data: Any) -> O:
        """Make the occurrence that an agent's answer names.

        Pydantic checks the payload as JSON, not in strict mode. So the string
        `"3"` agrees with an `int` field.

        Args:
            data: The answer: `{"event": ..., "payload": ...}`.

        Returns:
            The occurrence of the event, with its payload.

        Raises:
            OutcomeError: `data` does not agree with `outcome_schema()`.
        """
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
            if "payload" in body:
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
    """Mark a function as the routine of a state. The supervisor runs it for each
    task of the state.

    ```python
    @on_state(states.testing)
    def testing(ctx: TicketContext) -> events.passed | events.failed:
        return events.passed(Tests(passed=12, failed=0))
    ```

    Args:
        state: The state, from `contract.states`.

    Returns:
        A decorator. It returns the function unchanged, with a mark.

    Raises:
        TypeError: `state` is not a `StateRef`.
    """
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
