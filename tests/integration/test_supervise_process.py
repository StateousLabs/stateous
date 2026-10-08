"""The supervisor as a real process: the first signal waits for the running routine,
the second ends it with exit code 1, and `serve --supervise` runs both sides in one
process. On Windows the signal is Ctrl-Break, to a process in a new process group."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, update

from stateous import Client
from stateous.client.client import ClientError
from stateous.control.store import tables

from .supervising import (
    compile_flow,
    control_plane,
    database,
    free_port,
    has_kind,
    init_folder,
    status_is,
    wait_for,
)

FLOW = """
from stateous import END, START, Workflow

flow = Workflow("slowflow")
slow = flow.state("slow")
flow.transition(START, slow)
flow.transition(slow, END, on=flow.event("done"))
"""

SLOW = """
import time
from pathlib import Path

from contract import events, states

from stateous import Context, on_state


@on_state(states.slow)
def slow(ctx: Context) -> events.done:
    release = Path(__file__).with_name("release")
    while not release.exists():
        time.sleep(0.05)
    return events.done()
"""

STUBBORN = """
import asyncio

from contract import events, states

from stateous import Context, on_state


@on_state(states.slow)
async def slow(ctx: Context) -> events.done:
    while True:
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass  # this routine ignores its cancellation
"""


@contextmanager
def started(args: list[str], log: Path) -> Generator[subprocess.Popen[bytes]]:
    """`python -m stateous ARGS`, its output in `log`. Killed at the end if it still runs."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
    with log.open("wb") as out:
        process = subprocess.Popen(
            [sys.executable, "-m", "stateous", *args],
            stdout=out,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)


def interrupt(process: subprocess.Popen[bytes]) -> None:
    """What an operator's Ctrl-C (Ctrl-Break on Windows) sends."""
    if sys.platform == "win32":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        process.send_signal(signal.SIGINT)


def text_of(log: Path) -> str:
    return log.read_text(encoding="utf-8", errors="replace")


def says(log: Path, text: str) -> bool:
    return text in text_of(log)


def supervisor_folder(tmp_path: Path, url: str, routine: str = SLOW) -> Path:
    folder = tmp_path / "slow-supervisor"
    init_folder(url, folder)
    (folder / "slow.py").write_text(routine, encoding="utf-8")
    return folder


def test_the_first_signal_waits_for_the_running_routine_then_exits_0(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    log = tmp_path / "supervise.log"
    with control_plane(graph, database(tmp_path)) as url, Client(url) as client:
        folder = supervisor_folder(tmp_path, url)
        client.start("T-1")
        with started(["supervise", str(folder)], log) as process:
            wait_for(has_kind, client, "T-1", "task_claimed")
            interrupt(process)
            time.sleep(1.0)
            assert process.poll() is None, text_of(log)
            assert status_is(client, "T-1", "active")
            (folder / "release").touch()
            assert process.wait(timeout=30) == 0, text_of(log)
        assert status_is(client, "T-1", "completed")
    assert "stopping: no new tasks" in text_of(log)


def test_the_second_signal_exits_1_while_the_routine_still_runs(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    log = tmp_path / "supervise.log"
    with control_plane(graph, database(tmp_path)) as url, Client(url) as client:
        folder = supervisor_folder(tmp_path, url)
        client.start("T-1")
        with started(["supervise", str(folder)], log) as process:
            wait_for(has_kind, client, "T-1", "task_claimed")
            interrupt(process)
            time.sleep(0.5)
            interrupt(process)
            assert process.wait(timeout=15) == 1, text_of(log)
        assert status_is(client, "T-1", "active")  # the lease ends later; the task is retried
    assert "stopping now" in text_of(log)
    assert "Traceback" not in text_of(log)


def test_the_second_signal_exits_1_when_a_routine_ignores_its_cancellation(
    tmp_path: Path,
) -> None:
    graph = compile_flow(tmp_path, FLOW)
    log = tmp_path / "supervise.log"
    with control_plane(graph, database(tmp_path)) as url, Client(url) as client:
        folder = supervisor_folder(tmp_path, url, STUBBORN)
        client.start("T-1")
        with started(["supervise", str(folder)], log) as process:
            wait_for(has_kind, client, "T-1", "task_claimed")
            interrupt(process)
            time.sleep(0.5)
            interrupt(process)
            assert process.wait(timeout=30) == 1, text_of(log)
    assert "did not stop" in text_of(log)
    assert "Task was destroyed" not in text_of(log)
    assert "Traceback" not in text_of(log)


def answers(url: str) -> bool:
    try:
        with Client(url) as client:
            client.workflow()
    except ClientError:
        return False
    return True


def serve_args(tmp_path: Path, graph: Path, port: int, folder: Path) -> list[str]:
    return [
        "serve",
        str(graph),
        "--db",
        database(tmp_path, "serve"),
        "--listen",
        f"127.0.0.1:{port}",
        "--supervise",
        str(folder),
    ]


def released_folder(tmp_path: Path, graph: Path) -> Path:
    """A folder whose routine returns at once, made by `init` against another control plane."""
    with control_plane(graph, database(tmp_path, "init")) as init_url:
        folder = supervisor_folder(tmp_path, init_url)
    (folder / "release").touch()
    return folder


def test_serve_with_supervise_runs_both_sides_in_one_process(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    folder = released_folder(tmp_path, graph)
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    log = tmp_path / "serve.log"

    with started(serve_args(tmp_path, graph, port, folder), log) as process:
        wait_for(answers, url)
        with Client(url) as client:
            client.start("T-1")
            wait_for(status_is, client, "T-1", "completed")
        interrupt(process)
        assert process.wait(timeout=30) == 0, text_of(log)

    assert f"serving on {url}" in text_of(log)
    assert "stopped" in text_of(log)
    assert "Traceback" not in text_of(log)


def test_serve_with_supervise_stops_both_sides_when_the_hold_is_lost(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    folder = released_folder(tmp_path, graph)
    port = free_port()
    log = tmp_path / "serve.log"

    with started(serve_args(tmp_path, graph, port, folder), log) as process:
        wait_for(answers, f"http://127.0.0.1:{port}")
        engine = create_engine(database(tmp_path, "serve"))
        with engine.begin() as connection:
            connection.execute(update(tables.ControlPlane).values(serving="another-control-plane"))
        engine.dispose()
        assert process.wait(timeout=30) == 1, text_of(log)

    assert "another control plane took this database" in text_of(log)
    assert text_of(log).strip().splitlines()[-1].startswith("error: ")
    assert "Traceback" not in text_of(log)


def test_serve_with_supervise_refuses_a_folder_that_is_not_there(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    log = tmp_path / "serve.log"
    args = ["serve", str(graph), "--db", database(tmp_path), "--listen", f"127.0.0.1:{free_port()}"]

    with started([*args, "--supervise", str(tmp_path / "nothing")], log) as process:
        assert process.wait(timeout=30) == 1

    assert "is not a folder" in text_of(log)
    assert "Traceback" not in text_of(log)


IN_A_THREAD = """
import asyncio
import time
from pathlib import Path

from contract import events, states

from stateous import Context, on_state


def nap() -> None:
    Path(__file__).with_name("in-thread").touch()
    time.sleep(3600)


@on_state(states.slow)
async def slow(ctx: Context) -> events.done:
    await asyncio.to_thread(nap)
    return events.done()
"""


def test_the_second_signal_exits_1_while_a_routine_waits_for_a_thread(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    log = tmp_path / "supervise.log"
    with control_plane(graph, database(tmp_path)) as url, Client(url) as client:
        folder = supervisor_folder(tmp_path, url, IN_A_THREAD)
        client.start("T-1")
        with started(["supervise", str(folder)], log) as process:
            wait_for((folder / "in-thread").exists)
            interrupt(process)
            wait_for(says, log, "stopping: no new tasks")
            interrupt(process)
            began = time.monotonic()
            assert process.wait(timeout=30) == 1, text_of(log)
            took = time.monotonic() - began
    assert took < 10, text_of(log)
    assert "stopping now" in text_of(log)
    assert "Traceback" not in text_of(log)
