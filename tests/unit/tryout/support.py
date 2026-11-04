"""Helpers: a small workflow, its graph file and its supervisor's folder, in tmp_path."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from stateous.cli.main import app
from stateous.model.graph_file import dump_graph

from ...parallel import parallel_document

FLOW = """
from pydantic import BaseModel

from stateous import END, START, Workflow


class Job(BaseModel):
    title: str


flow = Workflow("job", context=Job)
work = flow.state("work", retries=RETRIES)
broken = flow.state("broken")
done = flow.event("done")
other = flow.event("other")

flow.transition(START, work)
flow.transition(work, END, on=done)
flow.transition(work, broken, on=other)
flow.transition(work, broken, on="error")
flow.transition(broken, END, on=done)
"""

HEAD = """
import asyncio

from contract import JobContext, events, states

from stateous import on_state
"""

WORK = """

@on_state(states.work)
async def work(ctx: JobContext) -> events.done | events.other | None:
"""

BROKEN = """

@on_state(states.broken)
def broken(ctx: JobContext) -> events.done:
"""


def run(args: list[str]) -> None:
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output


def job(
    tmp_path: Path,
    work: str | None,
    *,
    retries: int = 0,
    broken: bool = True,
    broken_body: str = "return events.done()",
) -> tuple[Path, Path]:
    """The graph file and the supervisor's folder of `job`. `work` is the body of the
    routine of `work` (an async def), or None for no routine of `work`."""
    project = tmp_path / "job"
    project.mkdir()
    flow = FLOW.replace("RETRIES", str(retries))
    (project / "flow.py").write_text(flow, encoding="utf-8")
    run(["compile", str(project / "flow.py")])
    graph = project / "build" / "job.json"
    folder = tmp_path / "job-supervisor"
    run(["init", "supervisor", str(graph), str(folder)])
    for stub in folder.glob("*.py"):
        if stub.name != "contract.py":
            stub.unlink()
    source = HEAD
    if work is not None:
        source += WORK + "".join(f"    {line}\n" for line in work.strip().splitlines())
    if broken:
        source += BROKEN + f"    {broken_body}\n"
    (folder / "routines.py").write_text(source, encoding="utf-8")
    return graph, folder


PARALLEL_ROUTINES = """
from contract import TicketContext, events, states

from stateous import on_state


@on_state(states.coding)
def coding(ctx: TicketContext) -> events.done:
    return events.done()
"""


def parallel(tmp_path: Path) -> tuple[Path, Path]:
    """The graph and folder of `sdlc` with parallel reviews; only `coding` has a routine."""
    graph = tmp_path / "sdlc.json"
    graph.write_text(dump_graph(parallel_document()), encoding="utf-8")
    folder = tmp_path / "sdlc-supervisor"
    run(["init", "supervisor", str(graph), str(folder)])
    for stub in folder.glob("*.py"):
        if stub.name != "contract.py":
            stub.unlink()
    (folder / "routines.py").write_text(PARALLEL_ROUTINES, encoding="utf-8")
    return graph, folder
