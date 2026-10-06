"""Whole-graph checks: mistakes that are visible only when the workflow is complete.

Declaration already refuses what it can see line by line (`model/workflow.py`).
These checks need the whole graph, so they run when compiling. They collect all
problems and report them together, so one compile shows everything to fix.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel

from stateous.compiler.exclusive import Where, check
from stateous.model.workflow import (
    END,
    ERROR,
    START,
    TIMEOUT,
    DefinitionError,
    State,
    Transition,
    Workflow,
)


def validate(flow: Workflow) -> None:
    """Raise one `DefinitionError` that lists every problem, or return."""
    problems = list(_problems(flow))
    if problems:
        listed = "\n".join(f"  - {problem}" for problem in problems)
        raise DefinitionError(f"workflow {flow.name!r} is not valid:\n{listed}")


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
    yield from _counted_events(flow)
    yield from _model_names(flow)


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
    """stateous-flow's rule: next to guarded transitions, an unguarded one must be
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


def _counted_events(flow: Workflow) -> Iterable[str]:
    known = set(flow.events) | {TIMEOUT, ERROR}
    for edge in flow.transitions:
        if edge.when is None:
            continue
        for event in sorted(edge.when.events - known):
            yield f"guard {edge.when.expression!r} counts {event!r}, which is not an event"


def _model_names(flow: Workflow) -> Iterable[str]:
    """The graph file and `contract.py` name each model by its class name, so two
    different models may not share one."""
    seen: dict[str, type[BaseModel]] = {}
    reported: set[str] = set()
    for model in [flow.context, *(event.payload for event in flow.events.values())]:
        if model is None:
            continue
        first = seen.setdefault(model.__name__, model)
        if first is not model and model.__name__ not in reported:
            reported.add(model.__name__)
            yield f"two different models are named {model.__name__!r}; give each its own name"


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
