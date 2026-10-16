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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel

from stateous.model.durations import duration_problem
from stateous.model.guards import Guard, GuardError, parse_guard

STARTED = "__started__"
"""The event a token leaves START on. The engine fires it as the token starts."""

TIMEOUT = "timeout"
"""Fired when a token stays in a state for longer than the state's `timeout`."""

ERROR = "error"
"""Fired when a task fails and has no retries left."""

JOINED = "joined"
"""Fired on a token in a parallel state when the state's join is met (see the
guide, "The join")."""

RESERVED_EVENTS = (TIMEOUT, ERROR, JOINED, STARTED)
"""Events Stateous fires itself. Transitions use them by name; no one declares them."""

JOINS = ("all", "any")
"""How a parallel state joins: when each child has ended, or when the first one has."""

MAX_TOKEN_ID = 128
"""The most characters in a token id. A child's id is its parent's id and more."""

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
    """A place a token can be. Entering it creates one task. Entering a parallel state
    starts one child token for each of its workflows instead (see the guide,
    "Nested and parallel work")."""

    name: str
    workflow: str
    """The declaring workflow's name."""
    timeout: str | None = None
    retries: int = 0
    parallel: tuple[Workflow, ...] = ()
    """The child workflows of a parallel state. Empty for a state with a task."""
    join: str = "all"
    """For a parallel state: `all` or `any` (see JOINS)."""
    inputs: tuple[str, ...] = ()
    """For a parallel state: the context keys that each child starts with."""


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
"""Where every token begins. Import it; do not declare it.

A transition from START takes no `on=`. The control plane leaves START at once, on
`__started__`. When START has more than one transition, their guards on the starting
context choose one."""

END = State("__end__", workflow="")
"""Where a token completes. Import it; do not declare it.

A token that enters END is `completed`. END has no task, and no transition leaves it."""


class Workflow:
    """A process: its states, its events, and the transitions between them.

    You write it in `flow.py`. `stateous compile` checks it and writes the graph
    file that a control plane serves. Each method refuses a mistake at once, with
    `DefinitionError`.

    ```python
    flow = Workflow("sdlc", context=Ticket)
    coding = flow.state("coding", timeout="6h", retries=2)
    testing = flow.state("testing", timeout="1h")
    failed = flow.event("failed", payload=Tests)
    flow.transition(testing, coding, on=failed, when="count('failed') < 3")
    ```
    """

    def __init__(self, name: str, *, context: type[BaseModel] | None = None) -> None:
        """Make an empty workflow.

        Args:
            name: The workflow's name: `a-z`, `0-9`, `_` and `-`, with a letter
                first, at most 64 characters. It is also the name of the graph file.
            context: The Pydantic model of each token's context. A token starts with
                it, and its routines read and write it as `ctx["key"]`. None: any
                JSON object.

        Raises:
            DefinitionError: The name is not valid, or `context` is not a Pydantic
                model class.
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

    def state(
        self,
        name: str,
        *,
        timeout: str | None = None,
        retries: int = 0,
        parallel: Sequence[Workflow] = (),
        join: str = "all",
        inputs: Sequence[str] = (),
    ) -> State:
        """Add a state to the workflow.

        When a token enters the state, the control plane makes one task. With
        `parallel`, it is a parallel state: it starts one child token for each
        workflow, and it has no task. See the docs, Concepts > Parallel and nested
        work.

        Args:
            name: The state's name: `a-z`, `0-9`, `_` and `-`, with a letter first.
                It is unique in the workflow.
            timeout: How long a token can stay in the state, for example `"30s"`,
                `"10m"`, `"6h"` or `"1d"`. Then the token gets the event `timeout`.
                None: no limit.
            retries: How many more times a failed task runs. Then the token gets
                the event `error`. A parallel state has no task, so no retries.
            parallel: The child workflows of a parallel state. When the join is
                met, the token gets the event `joined`.
            join: `"all"`: wait until each child ends. `"any"`: continue when the
                first child ends, and cancel the others.
            inputs: The context keys that each child's context starts with. They
                must give each field that a child's context requires.

        Returns:
            The state, to use in transitions.

        Raises:
            DefinitionError: The name is not valid, is used, or is Stateous's own.
                Or the timeout is not a duration, `retries` is not 0 or more, or
                `parallel`, `join` or `inputs` breaks a rule of a parallel state.
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
        children = self._children(name, parallel, retries)
        if join not in JOINS:
            raise DefinitionError(f"state {name!r}: join must be 'all' or 'any', not {join!r}")
        keys: object = inputs  # as given: a user can pass anything
        if isinstance(keys, str) or not all(
            isinstance(key, str) and key for key in cast("Sequence[object]", keys)
        ):
            raise DefinitionError(
                f"state {name!r}: inputs is a list of context keys, e.g. inputs=['repo']"
            )
        if not children and (join != "all" or inputs):
            raise DefinitionError(
                f"state {name!r}: join and inputs are for a parallel state; add parallel=[...]"
            )
        declared = State(
            name,
            workflow=self.name,
            timeout=timeout,
            retries=retries,
            parallel=children,
            join=join,
            inputs=tuple(inputs),
        )
        self.states[name] = declared
        return declared

    def _children(
        self, name: str, parallel: Sequence[Workflow], retries: int
    ) -> tuple[Workflow, ...]:
        """The child workflows of the state `name`, checked."""
        given: object = parallel  # as given: a user can pass anything
        if isinstance(given, Workflow) or not all(
            isinstance(child, Workflow) for child in cast("Sequence[object]", given)
        ):
            raise DefinitionError(
                f"state {name!r}: parallel takes a list of workflows, e.g. parallel=[review]"
            )
        children = tuple(parallel)
        if any(child is self for child in children):
            raise DefinitionError(f"state {name!r}: a workflow cannot run itself")
        names = [child.name for child in children]
        for child in names:
            if names.count(child) > 1:
                raise DefinitionError(
                    f"state {name!r} runs the workflow {child!r} two times; each child "
                    "needs a workflow of its own"
                )
        if children and retries:
            raise DefinitionError(
                f"state {name!r}: a parallel state has no task, so it has no retries"
            )
        return children

    def event(self, name: str, *, payload: type[BaseModel] | None = None) -> Event:
        """Add an event to the workflow. An event is an outcome: only an event
        moves a token.

        Args:
            name: The event's name, unique in the workflow. Do not declare
                Stateous's own events, `timeout`, `error` and `joined`: a
                transition uses them by name.
            payload: The Pydantic model of the data that the event carries. None:
                no data.

        Returns:
            The event, to use in transitions.

        Raises:
            DefinitionError: The name is not valid, is used, or is Stateous's own.
                Or the payload is not a Pydantic model class.
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
        """Add a transition: the event `on` moves a token from `source` to
        `target`. The events that a state can emit are the events of its
        transitions.

        Args:
            source: The state that the token is in, or `START`.
            target: The state that the token moves to, or `END`.
            on: The event, or the name of one of Stateous's own events:
                `"timeout"`, `"error"` or `"joined"`. Leave it out from `START`.
            when: A guard that reads `ctx['key']`, `payload.field` and
                `count('event')`. Two guards on one state and event must not
                overlap: `stateous compile` checks this.
            otherwise: True for the fallback: the control plane takes it when no
                guard on the same state and event is true. It has no `when`.

        Returns:
            The transition.

        Raises:
            DefinitionError: A state is not of this workflow, or the event is
                missing or not allowed here. Or the guard does not parse, or it
                reads a field that the payload does not have. Or two transitions
                have no guard on the same state and event.
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
        if event.name == JOINED and not source.parallel:
            raise DefinitionError(
                f"only a parallel state is left on {JOINED!r}; {source.name!r} has no "
                "parallel=[...]"
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
        if guard.branches and event.name != JOINED:
            raise DefinitionError(
                f"guard {when!r}: branches.all() and branches.any() are for transitions "
                f"on {JOINED!r}"
            )
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
