"""The stops of a try-out at a state that waits for the world.

A token waits for the world when nothing in the try-out moves it on: its state has no
routine, its routine returned "waiting" (for example for a human approval), or its
routine set a timer. The try-out does not answer for the world. It stops, and names
what the state waits for.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any, cast

from stateous.control.engine import HistoryEntry, Token
from stateous.control.graph import OWN_EVENTS, TIMEOUT
from stateous.tryout.report import PAGE_OF, Reason, Stop

TASK_KINDS = frozenset({"task_queued", "task_retrying", "task_waiting", "task_done", "task_failed"})


def waiting_stop(
    document: dict[str, Any],
    served: Collection[str],
    family: list[Token],
    workflows: Mapping[str, str],
    histories: Mapping[str, list[HistoryEntry]],
) -> Stop | None:
    """The first active token of `family` that waits for the world, as a stop; or None.

    `served` are the states that the supervisor claims. `workflows` gives the child
    workflow of each child token. Call it only when no task runs."""
    for token in family:
        if token.status != "active":
            continue
        workflow = workflows.get(token.token)
        body = document if workflow is None else document["workflows"][workflow]
        spec = cast("dict[str, Any] | None", body["states"].get(token.state))
        if spec is None or spec.get("parallel"):
            continue  # START, END, or a parallel state that waits for its children
        name = token.state if workflow is None else f"{workflow}/{token.state}"
        visit = _this_visit(histories[token.token], token.state)
        tasks = [entry.kind for entry in visit if entry.kind in TASK_KINDS]
        if not tasks:
            continue
        events = leaving(body, token.state)
        if tasks[-1] in ("task_queued", "task_retrying") and name not in served:
            return _stop(
                "no_routine",
                token,
                name,
                f"{token.token} waits at {name}, and no routine of the supervisor serves "
                f"it; write its routine, or answer for it with --fire {name}=EVENT "
                f"(events: {_names(events)})",
            )
        if tasks[-1] == "task_waiting":
            timers = _pending_timers(visit)
            if timers:
                shown = ", ".join(f"{event} at {due}" for event, due in timers)
                return _stop(
                    "waits_timer",
                    token,
                    name,
                    f"{token.token} waits at {name} for a timer ({shown}) or for an event "
                    f"from outside ({_names(events)}); ask the developer, then run again "
                    f"with --fire {name}=EVENT",
                )
            return _stop(
                "waits_outside",
                token,
                name,
                f"{token.token} waits at {name} for an event from outside "
                f"({_names(events)}); ask the developer, then run again "
                f"with --fire {name}=EVENT",
            )
    return None


def _stop(reason: Reason, token: Token, state: str, sentence: str) -> Stop:
    return Stop(reason, token.token, state, sentence, PAGE_OF[reason])


def _this_visit(history: list[HistoryEntry], state: str) -> list[HistoryEntry]:
    """The entries since the token last entered `state`."""
    for index in range(len(history) - 1, -1, -1):
        entry = history[index]
        if entry.kind == "entered" and entry.data.get("state") == state:
            return history[index + 1 :]
    return history


def leaving(body: dict[str, Any], state: str) -> list[str]:
    """The events that can come from outside and leave `state`, in declaration order."""
    found: list[str] = []
    for transition in cast("list[dict[str, Any]]", body["transitions"]):
        event = cast("str | None", transition.get("on"))
        leaves = transition["from"] == state and event is not None
        if leaves and event not in OWN_EVENTS and event not in found:
            found.append(cast("str", event))
    return found


def _pending_timers(visit: list[HistoryEntry]) -> list[tuple[str, str]]:
    """The timers of this visit that have not fired: (event, due time)."""
    pending: list[tuple[str, str]] = []
    for entry in visit:
        event = str(entry.data.get("event", TIMEOUT))
        if entry.kind == "timer_set":
            pending.append((event, str(entry.data.get("due_at"))))
        elif entry.kind == "timer_fired":
            pending = [timer for timer in pending if timer[0] != event]
    return pending


def _names(events: list[str]) -> str:
    return ", ".join(events) or "none"
