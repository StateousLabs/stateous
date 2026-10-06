"""Declaring a workflow: its states, its events, and the transitions between them.

This is the authoring API behind `flow.py`. It records what the author declares,
and refuses each mistake it can see at once. Checks that need the whole graph (a
state that cannot be reached, guards that overlap) run when compiling
(`stateous.compiler.validate`).

Every workflow runs from `START` to `END`: Stateous's own states, imported and
never declared. A token begins at START and leaves it at once, on `__started__`,
which the engine fires itself. Entering END completes the token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel

from stateous.model.durations import duration_problem
from stateous.model.guards import Guard, GuardError, parse_guard

STARTED = "__started__"
"""The event a token leaves START on. The engine fires it as the token starts."""

TIMEOUT = "timeout"
"""Fired when a token stays in a state for longer than the state's `timeout`."""

ERROR = "error"
"""Fired when a task fails and has no retries left."""

RESERVED_EVENTS = (TIMEOUT, ERROR, STARTED)
"""Events Stateous fires itself. Transitions use them by name; no one declares them."""

NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
"""A workflow, state or event name: a-z, 0-9, `_` and `-`, starting with a letter,
at most 64 characters."""


class DefinitionError(Exception):
    """A workflow definition is wrong. Raised as soon as the mistake is made."""


@dataclass(frozen=True, eq=False)
class Event:
    """A kind of outcome. The only thing that moves a token."""

    name: str
    payload: type[BaseModel] | None
    """The model its occurrences carry; None for an event with no payload."""
    workflow: str | None
    """The declaring workflow; None for Stateous's own events."""


@dataclass(frozen=True, eq=False)
class State:
    """A place a token can be. Entering it creates one task."""

    name: str
    workflow: str
    """The declaring workflow's name."""
    timeout: str | None = None
    retries: int = 0


@dataclass(frozen=True, eq=False)
class Transition:
    """Where an event leads from a state."""

    source: State
    target: State
    event: Event
    when: Guard | None = None
    otherwise: bool = False


_RESERVED = {name: Event(name, payload=None, workflow=None) for name in RESERVED_EVENTS}

START = State("__start__", workflow="")
"""Where every token begins. Its transitions take no `on=`: the engine leaves it at
once, on `__started__`, and chooses among them by their guards on the starting
context."""

END = State("__end__", workflow="")
"""Entering it completes the token. No task is queued there, and nothing leaves it."""


class Workflow:
    """A process: states, the events that can happen, and where each one leads.

    Written in `flow.py`. `stateous compile` turns it into the graph file that a
    control plane serves:

        flow = Workflow("sdlc", context=Ticket)
        testing = flow.state("testing", timeout="1h")
        failed = flow.event("failed", payload=Tests)
        flow.transition(testing, coding, on=failed, when="count('failed') < 3")
    """

    def __init__(self, name: str, *, context: type[BaseModel] | None = None) -> None:
        """Args:
        name: The workflow's name. It names the graph file.
        context: The model of each token's context: what a token starts with, and
            what its routines read and write as `ctx["key"]`. None: any object.
        """
        _check_name("workflow", name)
        _check_model(f"workflow {name!r}: context", context)
        self.name = name
        self.context = context
        self.states: dict[str, State] = {}
        """The declared states, by name, in declaration order."""
        self.events: dict[str, Event] = {}
        """The declared events, by name. Stateous's own events are not among them."""
        self.transitions: list[Transition] = []
        """The declared transitions, in declaration order."""

    def state(self, name: str, *, timeout: str | None = None, retries: int = 0) -> State:
        """Declare a state. Entering it creates one task.

        Args:
            name: Unique in the workflow.
            timeout: After this long in the state, the token gets `timeout`:
                `30s`, `10m`, `6h`, `1d`. None: no limit.
            retries: How many more tries a failed task gets. Then the token gets
                `error`.

        Raises:
            DefinitionError: The name is not valid, taken, or Stateous's own; the
                timeout is not a duration; retries is not a whole number, 0 or more.
        """
        if name in (START.name, END.name):
            raise DefinitionError(
                f"state {name!r} is Stateous's own; import START and END instead of declaring them"
            )
        _check_name("state", name)
        if name in self.states:
            raise DefinitionError(f"state {name!r} is already declared")
        if timeout is not None and (problem := duration_problem(timeout)):
            raise DefinitionError(f"state {name!r}: {problem}")
        if type(retries) is not int or retries < 0:
            raise DefinitionError(
                f"state {name!r}: retries must be a whole number, 0 or more, not {retries!r}"
            )
        declared = State(name, workflow=self.name, timeout=timeout, retries=retries)
        self.states[name] = declared
        return declared

    def event(self, name: str, *, payload: type[BaseModel] | None = None) -> Event:
        """Declare an event: an outcome, the only thing that moves a token.

        Args:
            name: Unique in the workflow, and not one of Stateous's own events
                (`timeout`, `error`), which transitions use by name.
            payload: The model of the data each occurrence carries. None: nothing.

        Raises:
            DefinitionError: The name is not valid, taken, or Stateous's own; the
                payload is not a pydantic model class.
        """
        if name in _RESERVED:
            raise DefinitionError(
                f"event {name!r} is Stateous's own; transitions use it by name, "
                "without a declaration"
            )
        _check_name("event", name)
        if name in self.events:
            raise DefinitionError(f"event {name!r} is already declared")
        _check_model(f"event {name!r}: payload", payload)
        declared = Event(name, payload=payload, workflow=self.name)
        self.events[name] = declared
        return declared

    def transition(
        self,
        source: State,
        target: State,
        *,
        on: Event | str | None = None,
        when: str | None = None,
        otherwise: bool = False,
    ) -> Transition:
        """Declare where `on` leads from `source`. The events a state may emit
        follow from the transitions out of it.

        Args:
            source: The state the token is in, or `START`.
            target: The state the token moves to, or `END`.
            on: The event, or the name of one of Stateous's own: `"timeout"`,
                `"error"`. Left out from `START`.
            when: A guard over `ctx['key']`, `payload.field` and `count('event')`.
                Guards on one state and event must never overlap; compiling checks.
            otherwise: The fallback, taken when no guard on the same state and
                event holds. It takes no `when`.

        Raises:
            DefinitionError: A state is not this workflow's; the event is missing
                or not allowed here; the guard does not parse or reads a field the
                payload does not have; the transition is ambiguous with one
                already declared.
        """
        source, target = self._own(source), self._own(target)
        event = self._taken_on(source, target, on)
        if when is not None and otherwise:
            raise DefinitionError("a fallback (otherwise=True) has no guard; drop `when`")
        guard = self._guard(when, event)
        if source is START and guard is not None:
            _refuse_before_anything_happened(guard)
        edge = Transition(source, target, event, guard, otherwise)
        self._reject_ambiguity(edge)
        self.transitions.append(edge)
        return edge

    def _taken_on(self, source: State, target: State, on: Event | str | None) -> Event:
        """The event a transition is taken on, after the checks for START and END."""
        if target is START:
            raise DefinitionError("nothing may lead into START: a token is there only as it starts")
        if source is END:
            raise DefinitionError("nothing may leave END: entering it completes the token")
        if source is START:
            if target is END:
                raise DefinitionError(
                    "a token cannot go straight from START to END; it needs a state to be in"
                )
            if on is not None:
                raise DefinitionError(
                    "a transition from START takes no `on=`: the engine takes it as the token "
                    "starts. Choose among several with `when=` and `otherwise=True`."
                )
            return _RESERVED[STARTED]
        if on is None:
            raise DefinitionError(
                f"a transition from {source.name!r} needs `on=`: the event it is taken on"
            )
        event = self._event(on)
        if event.name == STARTED:
            raise DefinitionError(
                f"{STARTED!r} is Stateous's own: only a transition from START is taken on it"
            )
        return event

    def _own(self, state: State) -> State:
        if state is START or state is END:
            return state
        if state.workflow != self.name or self.states.get(state.name) is not state:
            raise DefinitionError(
                f"state {state.name!r} belongs to workflow {state.workflow!r}, not {self.name!r}"
            )
        return state

    def _event(self, event: Event | str) -> Event:
        name = event if isinstance(event, str) else event.name
        found = _RESERVED.get(name) or self.events.get(name)
        if found is None or (isinstance(event, Event) and found is not event):
            raise DefinitionError(f"event {name!r} is not declared in workflow {self.name!r}")
        return found

    def _guard(self, when: str | None, event: Event) -> Guard | None:
        if when is None:
            return None
        try:
            guard = parse_guard(when)
        except GuardError as error:
            raise DefinitionError(str(error)) from None
        if event.name != STARTED:  # START's guards are checked on their own
            for name in sorted(guard.payload_fields):
                _require_field(event, name)
        return guard

    def _reject_ambiguity(self, edge: Transition) -> None:
        if edge.when is not None:
            return
        for other in self.transitions:
            if other.source is edge.source and other.event is edge.event and other.when is None:
                what = "START" if edge.source is START else _edge_name(edge)
                raise DefinitionError(
                    f"{what} is ambiguous: two transitions have no guard. Give one a "
                    "`when`, and mark the fallback otherwise=True."
                )


def _edge_name(edge: Transition) -> str:
    return f"{edge.event.name!r} from {edge.source.name!r}"


def _refuse_before_anything_happened(guard: Guard) -> None:
    """START's guards choose where a token begins, on its starting context: no
    event has been routed, so there is no payload and no count."""
    if guard.payload_fields:
        raise DefinitionError(
            f"guard {guard.expression!r}: START's guards read the context only; "
            "a token has no event, and so no payload, as it starts"
        )
    if guard.events:
        raise DefinitionError(
            f"guard {guard.expression!r}: START's guards cannot count events; "
            "nothing has happened yet as a token starts"
        )


def _require_field(event: Event, name: str) -> None:
    if event.payload is None or name not in event.payload.model_fields:
        raise DefinitionError(f"{event.name!r} payload has no field {name!r}")


def _check_name(what: str, name: str) -> None:
    is_text = isinstance(name, str)  # pyright: ignore[reportUnnecessaryIsInstance]
    if not is_text or NAME.fullmatch(name) is None:
        raise DefinitionError(
            f"{what} name {name!r} is not valid: use a-z, 0-9, '_' and '-', "
            "starting with a letter, at most 64 characters"
        )


def _check_model(what: str, model: type[BaseModel] | None) -> None:
    if model is None:
        return
    is_class = isinstance(model, type)  # pyright: ignore[reportUnnecessaryIsInstance]
    if not (is_class and issubclass(model, BaseModel)):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise DefinitionError(f"{what} must be a pydantic model class, not {model!r}")
