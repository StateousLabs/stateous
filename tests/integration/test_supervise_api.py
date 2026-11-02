"""`stateous.supervise`: a supervisor over any transport, here the in-process one."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from stateous import ControlPlane, supervise
from stateous.control.inprocess import InProcess
from stateous.supervisor.supervisor import SupervisorError

from .supervising import compile_flow, database, init_folder

FLOW = """
from pydantic import BaseModel

from stateous import END, START, Workflow


class Job(BaseModel):
    title: str


flow = Workflow("job", context=Job)
work = flow.state("work")
done = flow.event("done")

flow.transition(START, work)
flow.transition(work, END, on=done)
"""

WORK = """
from contract import JobContext, events, states

from stateous import on_state


@on_state(states.work)
def work(ctx: JobContext) -> events.done:
    return events.done()
"""


def folder_for(tmp_path: Path, graph: Path) -> Path:
    folder = tmp_path / "job-supervisor"
    init_folder(str(graph), folder)
    (folder / "work.py").write_text(WORK, encoding="utf-8")
    return folder


def test_in_process_runs_a_token_to_the_end(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    folder = folder_for(tmp_path, graph)

    async def main() -> int:
        with ControlPlane(graph, database=database(tmp_path), migrate=True) as plane:
            plane.start("J-1", {"title": "first"})
            stop = asyncio.Event()

            async def stop_when_done() -> None:
                while plane.get("J-1").status != "completed":
                    await asyncio.sleep(0.05)
                stop.set()

            watcher = asyncio.create_task(stop_when_done())
            code = await asyncio.wait_for(
                supervise(folder, InProcess(plane), stop=stop), timeout=30
            )
            await watcher
            return code

    assert asyncio.run(main()) == 0


def test_supervise_refuses_a_folder_for_another_graph(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    folder = folder_for(tmp_path, graph)
    other = json.loads(graph.read_text(encoding="utf-8"))
    other["states"]["work"]["timeout"] = "1h"  # another checksum
    other_graph = tmp_path / "other.json"
    other_graph.write_text(json.dumps(other), encoding="utf-8")

    async def main() -> int:
        with ControlPlane(other_graph, database=database(tmp_path, "o"), migrate=True) as plane:
            return await supervise(folder, InProcess(plane))

    with pytest.raises(SupervisorError, match="is for another version of the workflow"):
        asyncio.run(main())
