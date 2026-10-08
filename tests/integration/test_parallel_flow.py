"""A parallel state end to end: `compile` a flow.py that declares its child workflows,
`serve` it, `init` a supervisor's folder, and `supervise` it. The supervisor claims
the children's tasks as `<workflow>/<state>` (design section 8)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from stateous import Client
from stateous.cli.main import app
from stateous.client import Refused

from .supervising import (
    compile_flow,
    control_plane,
    database,
    has_kind,
    init_folder,
    status_is,
    supervising,
    wait_for,
)

FLOW = """
from pydantic import BaseModel

from stateous import END, START, Workflow


class Ticket(BaseModel):
    repo: str
    branch: str = "main"


class Review(BaseModel):
    repo: str
    branch: str


def review(name: str) -> Workflow:
    child = Workflow(name, context=Review)
    triage = child.state("triage")
    approved = child.state("approved")
    rejected = child.state("rejected")
    recorded = child.event("recorded")
    child.transition(START, triage)
    child.transition(triage, approved, on=child.event("lgtm"))
    child.transition(triage, rejected, on=child.event("deny"))
    child.transition(approved, END, on=recorded)
    child.transition(rejected, END, on=recorded)
    return child


security = review("security")
design = review("design")

flow = Workflow("sdlc", context=Ticket)
coding = flow.state("coding")
reviews = flow.state("reviews", parallel=[security, design], inputs=["repo", "branch"])
merging = flow.state("merging")
flow.transition(START, coding)
flow.transition(coding, reviews, on=flow.event("done"))
flow.transition(
    reviews,
    merging,
    on="joined",
    when="branches.all(status == 'completed' and final == 'approved')",
)
flow.transition(reviews, coding, on="joined", otherwise=True)
flow.transition(merging, END, on=flow.event("merged"))
"""

ROUTINES = """
from contract import ReviewContext, TicketContext, events, states

from stateous import on_state


@on_state(states.coding)
def coding(ctx: TicketContext) -> events.done:
    return events.done()


@on_state(states.merging)
def merging(ctx: TicketContext) -> events.merged:
    return events.merged()


@on_state(states.security.triage)
def security_triage(ctx: ReviewContext) -> events.security.lgtm:
    return events.security.lgtm()


@on_state(states.security.approved)
def security_approved(ctx: ReviewContext) -> events.security.recorded:
    return events.security.recorded()


@on_state(states.security.rejected)
def security_rejected(ctx: ReviewContext) -> events.security.recorded:
    return events.security.recorded()


@on_state(states.design.triage)
def design_triage(ctx: ReviewContext) -> events.design.lgtm | None:
    if ctx["repo"] == "acme/slow":
        return None  # a person reviews it later
    return events.design.lgtm()


@on_state(states.design.approved)
def design_approved(ctx: ReviewContext) -> events.design.recorded:
    return events.design.recorded()


@on_state(states.design.rejected)
def design_rejected(ctx: ReviewContext) -> events.design.recorded:
    return events.design.recorded()
"""


def supervisor_folder(tmp_path: Path) -> Path:
    """A supervisor's folder with one file of routines, for each state of each workflow."""
    folder = tmp_path / "supervisor"
    folder.mkdir()
    (folder / "routines.py").write_text(ROUTINES, encoding="utf-8")
    return folder


def test_a_parallel_state_runs_its_children_end_to_end(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    folder = supervisor_folder(tmp_path)

    with control_plane(graph, database(tmp_path)) as url:
        init_folder(url, folder)
        written = sorted(path.name for path in folder.glob("*.py"))
        with supervising(folder), Client(url) as client:
            client.start("T-1", {"repo": "acme/api"})
            wait_for(status_is, client, "T-1", "completed")
            children = client.tokens(parent="T-1")
            history = client.history("T-1")

    assert written == ["contract.py", "routines.py"]  # each state has its routine: no stubs
    assert sorted(child["token"] for child in children) == [
        "T-1.reviews.1.design",
        "T-1.reviews.1.security",
    ]
    assert {(child["status"], child["parent"]) for child in children} == {("completed", "T-1")}
    assert [entry["kind"] for entry in history].count("branch_started") == 2
    join = next(
        entry for entry in history
        if entry["kind"] == "event_accepted" and entry["data"]["event"] == "joined"
    )  # fmt: skip
    finals = sorted(
        (branch["workflow"], branch["status"], branch["final"])
        for branch in join["data"]["payload"]["branches"]
    )
    assert finals == [("design", "completed", "approved"), ("security", "completed", "approved")]


def test_a_waiting_child_is_steered_through_its_parent(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    folder = supervisor_folder(tmp_path)
    slow = "T-2.reviews.1.design"

    with control_plane(graph, database(tmp_path)) as url:
        init_folder(url, folder)
        with supervising(folder), Client(url) as client:
            client.start("T-2", {"repo": "acme/slow"})
            wait_for(has_kind, client, "T-2", "branch_started")  # then the children exist
            wait_for(has_kind, client, slow, "task_waiting")
            wait_for(status_is, client, "T-2.reviews.1.security", "completed")
            with pytest.raises(Refused) as refused:
                client.cancel(slow)
            client.pause("T-2")
            paused = client.get(slow)["status"]
            client.resume("T-2")
            client.cancel("T-2")
            shown = CliRunner().invoke(app, ["token", "show", "T-2", "--url", url])

    assert refused.value.status == 409
    assert paused == "paused"
    assert shown.exit_code == 0, shown.output
    lines = shown.stdout.splitlines()
    assert f"  child {slow}: triage, cancelled" in lines
    assert "  child T-2.reviews.1.security: approved, completed" in lines
