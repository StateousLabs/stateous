"""Helpers: a real control plane on a free port, `init` through the CLI, a
supervisor in a thread of its own, and waits."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import uvicorn
from typer.testing import CliRunner

from stateous import Client
from stateous.cli.main import app
from stateous.cli.serving import prepare_serve
from stateous.supervisor.folder import load_folder
from stateous.supervisor.remote import ControlPlaneApi
from stateous.supervisor.supervisor import Supervisor, open_supervisor


def compile_flow(tmp_path: Path, source: str) -> Path:
    """Write `flow.py`, run `stateous compile` on it, and return the graph file."""
    project = tmp_path / "flow"
    project.mkdir()
    (project / "flow.py").write_text(source, encoding="utf-8")
    result = CliRunner().invoke(app, ["compile", str(project / "flow.py")])
    assert result.exit_code == 0, result.output
    (graph,) = (project / "build").glob("*.json")
    return graph


def database(tmp_path: Path, name: str = "cp") -> str:
    return f"sqlite:///{(tmp_path / f'{name}.db').as_posix()}"


def wait_for(condition: Callable[..., bool], *args: Any, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition(*args):
        assert time.monotonic() < deadline, f"timed out waiting for {condition.__name__}{args}"
        time.sleep(0.05)


def server_ready(server: uvicorn.Server, thread: threading.Thread) -> bool:
    return server.started or not thread.is_alive()


@contextmanager
def control_plane(graph: Path, db: str) -> Generator[str]:
    """`stateous serve GRAPH --db DB` on a free port of 127.0.0.1, in a thread. Its URL."""
    serving = prepare_serve(graph, db, "127.0.0.1:0")
    server = uvicorn.Server(serving.config)
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [serving.listener]}, daemon=True
    )
    thread.start()
    try:
        wait_for(server_ready, server, thread)
        assert server.started, "the control plane did not start"
        yield serving.url
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        serving.close()


def init_folder(url: str, folder: Path) -> None:
    result = CliRunner().invoke(app, ["init", url, str(folder)])
    assert result.exit_code == 0, result.output


def kinds(client: Client, token: str) -> list[str]:
    return [entry["kind"] for entry in client.history(token)]


def has_kind(client: Client, token: str, kind: str) -> bool:
    return kind in kinds(client, token)


def status_is(client: Client, token: str, status: str) -> bool:
    return client.get(token)["status"] == status


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def supervising(folder: Path) -> Generator[Supervisor]:
    """The supervisor of `folder`, running in a thread with its own loop: heartbeats
    every 0.2 seconds, claims every 0.05 seconds when idle. Stopped at once at the end."""
    loaded = load_folder(folder)
    found: list[Supervisor] = []
    failed: list[BaseException] = []
    ready = threading.Event()

    async def main() -> None:
        api = ControlPlaneApi(loaded.config.url, backoff=(0.05,))
        try:
            supervisor = await open_supervisor(loaded, api, heartbeat_every=0.2, idle=0.05)
            found.append(supervisor)
            ready.set()
            try:
                await supervisor.run()
            finally:
                supervisor.close()
        except Exception as error:
            failed.append(error)
        finally:
            ready.set()
            await api.aclose()

    def in_thread() -> None:
        asyncio.run(main())

    thread = threading.Thread(target=in_thread, daemon=True)
    thread.start()
    assert ready.wait(20), "the supervisor did not start"
    assert found, f"the supervisor did not start: {failed!r}"
    try:
        yield found[0]
    finally:
        found[0].on_signal()
        found[0].on_signal()
        thread.join(timeout=20)
        assert not thread.is_alive(), "the supervisor did not stop"
