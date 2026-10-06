"""The `stateous` command. Phase 1 has `compile`; later phases add the others."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any, NoReturn, cast

import typer

from stateous.cli.serving import (
    DEFAULT_DB,
    DEFAULT_LISTEN,
    GracefulServer,
    ServeRefused,
    configure_logging,
    prepare_serve,
)
from stateous.cli.tokens import token_app
from stateous.client.client import Client, ClientError
from stateous.compiler.codegen import CodegenError, generate_contract, generate_stub
from stateous.compiler.compile import compile_workflow
from stateous.compiler.load import LoadError, load_workflow
from stateous.model.graph_file import dump_graph
from stateous.model.workflow import DefinitionError
from stateous.supervisor.folder import Folder, FolderError, load_folder, write_folder
from stateous.supervisor.remote import ControlPlaneApi
from stateous.supervisor.supervisor import SupervisorError, open_supervisor, stop_on_signals

log = logging.getLogger("stateous.cli")

LEFTOVER_WAIT = 2.0
"""After the supervisor stopped: seconds to wait for the tasks still on the loop
(for example a routine that ignores its cancellation). Then the command exits."""

app = typer.Typer(
    help="A process as a graph of states. A control plane keeps every token durable.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """A process as a graph of states. A control plane keeps every token durable."""


@app.command("compile")
def compile_command(
    flow: Annotated[Path, typer.Argument(help="The workflow's file, e.g. sdlc/flow.py.")],
    out: Annotated[
        Path | None,
        typer.Option(
            "--out", help="The graph file to write. Default: build/<workflow>.json next to FLOW."
        ),
    ] = None,
) -> None:
    """Check a workflow, and write its graph file."""
    try:
        workflow = load_workflow(flow)
        document = compile_workflow(workflow)
        generate_contract(document)  # a name that cannot be Python is found now, not at `init`
    except (LoadError, DefinitionError, CodegenError) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1) from None
    target = out if out is not None else flow.parent / "build" / f"{workflow.name}.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dump_graph(document), encoding="utf-8", newline="\n")
    except OSError as error:
        typer.echo(f"error: cannot write {target}: {error.strerror}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"wrote {target}")


@app.command()
def serve(
    graph: Annotated[
        Path,
        typer.Argument(help="The graph file that `stateous compile` wrote.", show_default=False),
    ],
    db: Annotated[str, typer.Option("--db", help="The database URL.")] = DEFAULT_DB,
    listen: Annotated[
        str, typer.Option("--listen", help="HOST:PORT to listen on.")
    ] = DEFAULT_LISTEN,
) -> None:
    """Run the control plane: the HTTP API and the clock, over one database."""
    configure_logging()
    try:
        serving = prepare_serve(graph, db, listen)
    except ServeRefused as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"serving on {serving.url} (Ctrl-C stops)", err=True)
    server = GracefulServer(serving.config)

    def stop() -> None:
        server.should_exit = True

    serving.on_lost(stop)
    try:
        server.run(sockets=[serving.listener])
    finally:
        serving.close()
    if serving.clock.lost:
        typer.echo(
            "error: another control plane took the database; this control plane stopped",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo("stopped", err=True)


def _exit_with(message: str) -> NoReturn:
    """One plain sentence to stderr, then exit code 1. Never a traceback."""
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(1)


def _log_to_stderr() -> None:
    """The supervisor's log lines on stderr, at INFO. `configure_logging` adds a
    handler only when there is none, so a second call adds no second handler. The
    HTTP libraries log only warnings."""
    configure_logging()
    for name in ("httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _run_bounded(main: Coroutine[Any, Any, int], *, leftover_wait: float = LEFTOVER_WAIT) -> int:
    """Run `main` on a new event loop, and return its exit code. As `asyncio.run`,
    but the end does not wait without limit: the tasks left on the loop are
    cancelled, and the loop waits at most `leftover_wait` seconds for them. So a
    routine that ignores its cancellation cannot stop the command from exiting."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(main)
    finally:
        try:
            left = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in left:
                task.cancel()
            if left:
                _, pending = loop.run_until_complete(asyncio.wait(left, timeout=leftover_wait))
                if pending:
                    log.warning("%d task(s) did not stop; exiting without them", len(pending))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            loop.close()


@app.command()
def init(
    url: Annotated[str, typer.Argument(help="The control plane, e.g. http://127.0.0.1:8080.")],
    folder: Annotated[Path, typer.Argument(help="The supervisor's folder. Made if absent.")],
) -> None:
    """Write a supervisor's folder: contract.py, a stub per state, stateous.yaml.
    Run it again to update contract.py."""
    try:
        with Client(url) as client:
            document = client.workflow()
    except ClientError as error:
        _exit_with(f"cannot fetch the workflow from {url}: {error}")

    def stub(state: str) -> str:
        return generate_stub(document, state)

    states = list(cast("dict[str, Any]", document["states"]))
    try:
        lines = write_folder(
            folder, url=url, contract=generate_contract(document), states=states, stub=stub
        )
    except OSError as error:
        _exit_with(f"cannot write {folder}: {error.strerror or error}")
    for line in lines:
        typer.echo(line)
    typer.echo(f"\nWrite the routines, then run: stateous supervise {folder}")


@app.command()
def supervise(
    folder: Annotated[Path, typer.Argument(help="The supervisor's folder.")],
) -> None:
    """Run the routines of a supervisor's folder. Ctrl-C once: take no new tasks and
    wait for the running routines. Twice: cancel them and exit."""
    _log_to_stderr()
    try:
        loaded = load_folder(folder)
        code = _run_bounded(_supervise(loaded))
    except (FolderError, SupervisorError) as error:
        _exit_with(str(error))
    except KeyboardInterrupt:  # Ctrl-C before the handlers were in place
        code = 1
    raise typer.Exit(code)


async def _supervise(folder: Folder) -> int:
    api = ControlPlaneApi(folder.config.url)
    try:
        supervisor = await open_supervisor(folder, api)
        with stop_on_signals(supervisor):
            return await supervisor.run()
    finally:
        await api.aclose()


app.add_typer(token_app, name="token")
