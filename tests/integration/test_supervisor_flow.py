"""A supervisor runs a small workflow against a real control plane: a def routine
that fails once and is retried, an async routine that saves memory and notes
before its report, a routine that returns None and sets a timer, an outcome
reported later through the Client, and a cancelled token whose async routine is
cancelled."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

from stateous import Client

from .supervising import (
    compile_flow,
    control_plane,
    database,
    has_kind,
    init_folder,
    kinds,
    status_is,
    supervising,
    wait_for,
)

FLOW = """
from pydantic import BaseModel

from stateous import END, START, Workflow


class Ticket(BaseModel):
    repo: str
    note: str | None = None


class Verdict(BaseModel):
    score: int


flow = Workflow("demo", context=Ticket)
triage = flow.state("triage", retries=1)
coding = flow.state("coding")
review = flow.state("review")

scored = flow.event("scored", payload=Verdict)
done = flow.event("done")
remind = flow.event("remind")
approved = flow.event("approved")

flow.transition(START, triage)
flow.transition(triage, coding, on=scored)
flow.transition(coding, review, on=done)
flow.transition(review, review, on=remind)
flow.transition(review, END, on=approved)
"""

TRIAGE = """
import threading

from contract import TicketContext, Verdict, events, states

from stateous import on_state

THREADS: list[str] = []


@on_state(states.triage)
def triage(ctx: TicketContext) -> events.scored:
    THREADS.append(threading.current_thread().name)
    if ctx.attempt == 1:
        raise RuntimeError("the scanner is down")
    ctx["note"] = "looked at it"
    return events.scored(Verdict(score=len(ctx["repo"])))
"""

CODING = """
import asyncio
import threading

from contract import TicketContext, events, states

from stateous import on_state

THREADS: list[str] = []
CANCELLED: list[str] = []


@on_state(states.coding)
async def coding(ctx: TicketContext) -> events.done:
    THREADS.append(threading.current_thread().name)
    ctx.memory["session"] = f"s-{ctx.token}"
    ctx.note("started the agent")
    if ctx["repo"] == "acme/slow":
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            CANCELLED.append(ctx.token)
            raise
    return events.done()
"""

REVIEW = """
from contract import TicketContext, events, states

from stateous import on_state


@on_state(states.review)
def review(ctx: TicketContext) -> None:
    if ctx.visit == 1:
        ctx.schedule(events.remind, after="1s")
    return None
"""


def listed(module: str, name: str) -> list[str]:
    return cast("list[str]", vars(sys.modules[module])[name])


def waits_twice(client: Client, token: str) -> bool:
    return kinds(client, token).count("task_waiting") == 2


def index_of(history: list[dict[str, Any]], kind: str, **data: Any) -> int:
    for number, entry in enumerate(history):
        if entry["kind"] == kind and all(entry["data"].get(k) == v for k, v in data.items()):
            return number
    raise AssertionError(f"no {kind} {data} in {[e['kind'] for e in history]}")


def test_a_supervisor_runs_a_workflow_against_a_real_control_plane(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    folder = tmp_path / "demo-supervisor"
    with control_plane(graph, database(tmp_path)) as url, Client(url) as client:
        init_folder(url, folder)
        for name, text in (("triage", TRIAGE), ("coding", CODING), ("review", REVIEW)):
            (folder / f"{name}.py").write_text(text, encoding="utf-8")

        with supervising(folder) as supervisor:
            client.start("T-1", {"repo": "acme/api"})
            client.start("T-2", {"repo": "acme/slow"})

            # T-2: its async routine runs until the token is cancelled.
            wait_for(has_kind, client, "T-2", "note")
            assert supervisor.running
            client.cancel("T-2")
            wait_for(listed("coding", "CANCELLED").__contains__, "T-2")

            # T-1: review waited, its timer fired, review waited again.
            wait_for(waits_twice, client, "T-1")
            history = client.history("T-1")
            review_task = [
                e["data"]["task"]
                for e in history
                if e["kind"] == "task_queued" and e["data"]["state"] == "review"
            ][-1]
            client.report(review_task, "approved")
            wait_for(status_is, client, "T-1", "completed")
            assert client.get("T-1")["context"]["note"] == "looked at it"
            other = client.history("T-2")
            assert client.get("T-2")["status"] == "cancelled"

    # The def routine ran in its own thread; it failed once, and the retry passed.
    threads = listed("triage", "THREADS")
    assert len(threads) == 4  # T-1 twice, T-2 twice
    assert all(name.startswith("stateous-routine-") for name in threads)
    errors = [e["data"]["error"] for e in history if e["kind"] == "task_failed"]
    assert errors == ["RuntimeError: the scanner is down"]
    assert "task_retrying" in [e["kind"] for e in history]

    # The async routine ran on the loop, not in a routine thread.
    assert all(not name.startswith("stateous-routine-") for name in listed("coding", "THREADS"))

    # The memory write and the note came before coding's event.
    memory = index_of(history, "memory_set", state="coding", memory_key="session")
    note = index_of(history, "note", text="started the agent")
    event = index_of(history, "event_accepted", event="done")
    assert memory < note < event

    # The timer was set with the report, and fired.
    assert index_of(history, "timer_set", event="remind") < index_of(
        history, "timer_fired", event="remind"
    )

    # T-2 was cancelled while coding ran; coding never reported.
    assert listed("coding", "CANCELLED") == ["T-2"]
    assert not any(
        e["kind"] == "event_accepted" and e["data"].get("event") == "done" for e in other
    )


COUNTING = """
from pydantic import BaseModel

from stateous import END, START, Workflow


class Tally(BaseModel):
    count: int = 0


flow = Workflow("tally", context=Tally)
work = flow.state("work")
flow.transition(START, work)
flow.transition(work, END, on=flow.event("done"))
"""

BREAKS_THE_SCHEMA = """
from contract import events, states

from stateous import Context, on_state


@on_state(states.work)
async def work(ctx: Context) -> events.done:
    ctx["count"] = "many"
    return events.done()
"""


def test_a_refused_report_fails_the_task_at_once_with_the_refusal(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, COUNTING)
    folder = tmp_path / "tally-supervisor"
    with control_plane(graph, database(tmp_path)) as url, Client(url) as client:
        init_folder(url, folder)
        (folder / "work.py").write_text(BREAKS_THE_SCHEMA, encoding="utf-8")
        with supervising(folder):
            client.start("T-1")
            wait_for(has_kind, client, "T-1", "task_failed")  # long before the 2-minute lease
        history = client.history("T-1")
        context = client.get("T-1")["context"]

    errors = [e["data"]["error"] for e in history if e["kind"] == "task_failed"]
    assert len(errors) == 1
    assert errors[0].startswith(
        "the control plane refused the report: the context changes break the schema"
    )
    assert context["count"] == 0
