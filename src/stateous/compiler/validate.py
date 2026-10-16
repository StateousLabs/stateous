"""Whole-graph checks: mistakes that are visible only when the workflow is complete.

Declaration already refuses what it can see line by line (`model/workflow.py`).
These checks need the whole graph, so they run when compiling. They collect all
problems and report them together, so one compile shows everything to fix. Each
child workflow of a parallel state gets the same checks, and some of its own
(see the docs, Concepts > Parallel and nested work).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from pydantic import BaseModel, PydanticUserError

from stateous.compiler.exclusive import Where, check
from stateous.model.workflow import (
    END,
    ERROR,
    JOINED,
    MAX_TOKEN_ID,
    START,
    TIMEOUT,
    DefinitionError,
    State,
    Transition,
    Workflow,
)

VISIT_DIGITS = 3
"""A child's id holds its parent's visit of the parallel state. The id length check
counts visits of up to this many digits: up to 999 visits."""

TOP_ID_ROOM = 64
"""The characters of a top token's id that each nesting must leave room for."""


def validate(flow: Workflow) -> None:
    """Raise one `DefinitionError` that lists every problem, or return."""
    problems = list(_nesting(flow))
    if not problems:  # a workflow that runs itself would make the checks below loop
        flows = [flow, *nested_workflows(flow)]
        problems += _problems(flow)
        for child in flows[1:]:
            own = [*_problems(child), *_start_fallback(child)]
            problems += [f"in child workflow {child.name!r}: {problem}" for problem in own]
        problems += _id_length(flow)
        problems += _model_names(flows)
        problems += _json_schemas(flows)
    if problems:
        listed = "\n".join(f"  - {problem}" for problem in problems)
        raise DefinitionError(f"workflow {flow.name!r} is not valid:\n{listed}")


def nested_workflows(flow: Workflow) -> list[Workflow]:
    """The child workflows that `flow`'s parallel states run, and their own children:
    each once, in the order it is first named. Only for a flow whose nesting is valid
    (no workflow runs itself)."""
    found: list[Workflow] = []

    def visit(current: Workflow) -> None:
        for state in current.states.values():
            for child in state.parallel:
                if all(child is not seen for seen in found):
                    found.append(child)
                    visit(child)

    visit(flow)
    return found


def _problems(flow: Workflow) -> Iterable[str]:
    if not flow.states:
        yield f"workflow {flow.name!r} has no states"
        return
    if not any(edge.source is START for edge in flow.transitions):
        yield (
            f"workflow {flow.name!r} has no transition from START; add one to the state "
            "every token begins in, e.g. flow.transition(START, <state>)"
        )
    if not any(edge.target is END for edge in flow.transitions):
        yield (
            f"workflow {flow.name!r} has no transition into END, so no token of it can "
            "complete; add one, e.g. flow.transition(<state>, END, on=<event>)"
        )
    yield from _unreachable(flow)
    yield from _fallbacks(flow)
    yield from _overlapping_guards(flow)
    yield from _context_keys(flow)
    yield from _inputs(flow)
    yield from _parallel_exits(flow)
    yield from _counted_events(flow)


def _unreachable(flow: Workflow) -> Iterable[str]:
    reached: set[str] = set()
    frontier: list[State] = [START]
    while frontier:
        current = frontier.pop()
        for edge in flow.transitions:
            if edge.source is current and edge.target.name not in reached:
                reached.add(edge.target.name)
                frontier.append(edge.target)
    for name in flow.states:
        if name not in reached:
            yield f"state {name!r} cannot be reached from START"


def _fallbacks(flow: Workflow) -> Iterable[str]:
    """Next to guarded transitions, an unguarded one must be
    the marked fallback. A guarded group without a fallback is allowed; at run
    time, an event that no guard matches is refused."""
    groups: dict[tuple[str, str], list[bool]] = {}
    unmarked: set[tuple[str, str]] = set()
    for edge in flow.transitions:
        key = (edge.source.name, edge.event.name)
        groups.setdefault(key, []).append(edge.when is not None)
        if edge.when is None and not edge.otherwise:
            unmarked.add(key)
    for (source, event), guarded in groups.items():
        if any(guarded) and (source, event) in unmarked:
            yield (
                f"{_group(source, event)} has guarded transitions and an unguarded one; "
                "mark the unguarded one otherwise=True"
            )


def _overlapping_guards(flow: Workflow) -> Iterable[str]:
    """Two guards on one state and event must be provably never both true. START's
    transitions are one such group."""
    groups: dict[tuple[str, str], list[Transition]] = {}
    for edge in flow.transitions:
        if edge.when is not None:
            groups.setdefault((edge.source.name, edge.event.name), []).append(edge)
    for (source, event), edges in groups.items():
        where = Where(event, flow.context, edges[0].event.payload)
        for i, first in enumerate(edges):
            for second in edges[i + 1 :]:
                assert first.when is not None and second.when is not None
                a, b = first.when.expression, second.when.expression
                verdict = check(first.when, second.when, where)
                if verdict.example is not None:
                    shown = ", ".join(f"{k} = {v!r}" for k, v in verdict.example.items())
                    yield (
                        f"{_group(source, event)}: the guards {a!r} and {b!r} can both be "
                        f"true, e.g. when {shown}; make them exclusive, or make one the "
                        "fallback (otherwise=True)"
                    )
                elif verdict.undecidable is not None:
                    yield (
                        f"{_group(source, event)}: cannot prove the guards {a!r} and {b!r} "
                        f"are never both true: {verdict.undecidable}; compare values with "
                        "literals, or make one the fallback (otherwise=True)"
                    )


def _context_keys(flow: Workflow) -> Iterable[str]:
    if flow.context is None:
        return
    schema = flow.context.__name__
    for edge in flow.transitions:
        if edge.when is not None:
            for key in _missing(flow.context, sorted(edge.when.context_keys)):
                yield f"{_where(edge)} reads ctx[{key!r}], which {schema} does not have"


def _inputs(flow: Workflow) -> Iterable[str]:
    """A parallel state's inputs are keys of the parent's context, and they give each
    child every field that its context model requires."""
    for state in flow.states.values():
        if flow.context is not None:
            schema = flow.context.__name__
            for key in _missing(flow.context, state.inputs):
                yield f"state {state.name!r} hands on {key!r}, which {schema} does not have"
        for child in state.parallel:
            if child.context is None:
                continue
            for key, info in child.context.model_fields.items():
                if info.is_required() and key not in state.inputs:
                    yield (
                        f"state {state.name!r} does not hand on {key!r}, which "
                        f"{child.name!r} requires; add it to inputs"
                    )


def _parallel_exits(flow: Workflow) -> Iterable[str]:
    """A parallel state has no task, so only its join can move its token on. It
    must leave on `joined`, and it can never leave on `error` (see the docs,
    Concepts > Parallel and nested work)."""
    for state in flow.states.values():
        if not state.parallel:
            continue
        on_joined = any(
            edge.source is state and edge.event.name == JOINED for edge in flow.transitions
        )
        on_error = any(
            edge.source is state and edge.event.name == ERROR for edge in flow.transitions
        )
        if not on_joined:
            yield f"parallel state {state.name!r} needs a transition on {JOINED!r}"
        if on_error:
            yield (
                f"parallel state {state.name!r} has no task, so {ERROR!r} can never fire; "
                "check 'branches' on 'joined'"
            )


def _counted_events(flow: Workflow) -> Iterable[str]:
    known = set(flow.events) | {TIMEOUT, ERROR, JOINED}
    for edge in flow.transitions:
        if edge.when is None:
            continue
        for event in sorted(edge.when.events - known):
            yield f"guard {edge.when.expression!r} counts {event!r}, which is not an event"


def _start_fallback(child: Workflow) -> Iterable[str]:
    """A child workflow starts when its parent enters a state. No one is there to be
    told that its start was refused, so a transition from START must always hold."""
    starts = [edge for edge in child.transitions if edge.source is START]
    if starts and all(edge.when is not None for edge in starts):
        yield (
            "START has no fallback: a child workflow is started by the state that runs it, "
            "and no one is told when no guard holds; leave one transition from START "
            "without a guard, or mark one otherwise=True"
        )


def _nesting(flow: Workflow) -> Iterator[str]:
    """No workflow runs itself further down, and two different workflows never share
    a name: the name is in the child's id, in claims and in contract.py."""
    seen: dict[str, Workflow] = {flow.name: flow}
    reported: set[str] = set()

    def visit(current: Workflow, path: tuple[Workflow, ...]) -> Iterator[str]:
        for state in current.states.values():
            for child in state.parallel:
                if any(child is above for above in path):
                    yield (
                        f"state {state.name!r} of {current.name!r} runs {child.name!r}, which "
                        f"runs {current.name!r} in turn; a workflow cannot run itself"
                    )
                    continue
                first = seen.setdefault(child.name, child)
                if first is not child:
                    if child.name not in reported:
                        reported.add(child.name)
                        yield (
                            f"two different workflows are named {child.name!r}; give each "
                            "its own name"
                        )
                    continue
                yield from visit(child, (*path, child))

    yield from visit(flow, (flow,))


def _id_length(flow: Workflow) -> Iterable[str]:
    """A child's id is `<parent>.<state>.<visit>.<workflow>`, and a token id has at
    most MAX_TOKEN_ID characters. The nesting must leave TOP_ID_ROOM characters for
    the top token's id, with visits of VISIT_DIGITS digits."""
    longest = _suffix(flow)
    if longest > MAX_TOKEN_ID - TOP_ID_ROOM:
        yield (
            f"the child token ids can be {longest} characters longer than the top token's "
            f"id, so with a top id of {TOP_ID_ROOM} characters they can pass "
            f"{MAX_TOKEN_ID}; use shorter state and workflow names, or nest less"
        )


def _suffix(flow: Workflow) -> int:
    """The most characters that the nesting below `flow` adds to a token's id."""
    longest = 0
    for state in flow.states.values():
        for child in state.parallel:
            own = len(f".{state.name}.{'9' * VISIT_DIGITS}.{child.name}")
            longest = max(longest, own + _suffix(child))
    return longest


def _models(flows: list[Workflow]) -> list[type[BaseModel]]:
    """Each context and payload model of the flows, once, in order."""
    found: list[type[BaseModel]] = []
    for flow in flows:
        for model in [flow.context, *(event.payload for event in flow.events.values())]:
            if model is not None and model not in found:
                found.append(model)
    return found


def _model_names(flows: list[Workflow]) -> Iterable[str]:
    """The graph file and `contract.py` name each model by its class name, so two
    different models may not share one, in a parent or in a child."""
    seen: dict[str, type[BaseModel]] = {}
    reported: set[str] = set()
    for model in _models(flows):
        first = seen.setdefault(model.__name__, model)
        if first is not model and model.__name__ not in reported:
            reported.add(model.__name__)
            yield f"two different models are named {model.__name__!r}; give each its own name"


def _json_schemas(flows: list[Workflow]) -> Iterable[str]:
    """The graph file holds each model as JSON Schema, so each must have one."""
    for model in _models(flows):
        try:
            model.model_json_schema()
        except PydanticUserError as error:
            reason = (error.message.strip().splitlines() or [type(error).__name__])[0]
            yield f"the model {model.__name__} cannot be written as JSON Schema: {reason}"


def _group(source: str, event: str) -> str:
    """A state and event whose transitions are checked together, as a message names them."""
    return "START" if source == START.name else f"{event!r} from {source!r}"


def _where(edge: Transition) -> str:
    if edge.source is START:
        return f"transition START --> {edge.target.name!r}"
    target = "END" if edge.target is END else repr(edge.target.name)
    return f"transition {edge.source.name!r} --{edge.event.name}--> {target}"


def _missing(model: type[BaseModel], keys: Iterable[str]) -> list[str]:
    return [key for key in keys if key not in model.model_fields]
