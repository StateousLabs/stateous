"""The `stateous` command. Phase 1 has `compile`; later phases add the others."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import FrameType
from typing import Annotated, Any, NoReturn, cast

import typer
import uvicorn

from stateous.cli.serving import (
    DEFAULT_DB,
    DEFAULT_LISTEN,
    GracefulServer,
    ServeRefused,
    Serving,
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
from stateous.supervisor.supervisor import (
    Supervisor,
    SupervisorError,
    open_supervisor,
    stop_on_signals,
)

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
    supervise_folder: Annotated[
        Path | None,
        typer.Option(
            "--supervise",
            help="Also run the supervisor of this folder, in this process (for development).",
        ),
    ] = None,
) -> None:
    """Run the control plane: the HTTP API and the clock, over one database."""
    configure_logging()
    folder: Folder | None = None
    if supervise_folder is not None:
        try:
            folder = load_folder(supervise_folder)
        except FolderError as error:
            _exit_with(str(error))
    try:
        serving = prepare_serve(graph, db, listen)
    except ServeRefused as error:
        _exit_with(str(error))
    typer.echo(f"serving on {serving.url} (Ctrl-C stops)", err=True)
    code = 0
    try:
        if folder is None:
            server = GracefulServer(serving.config)

            def stop() -> None:
                server.should_exit = True

            serving.on_lost(stop)
            server.run(sockets=[serving.listener])
        else:
            _log_to_stderr()
            code = _serve_with_supervisor(serving, folder)
    finally:
        serving.close()
    if serving.clock.lost:
        _exit_with("another control plane took the database; this control plane stopped")
    if code != 0:
        raise typer.Exit(code)
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
                    loop.set_exception_handler(_quiet_about(pending))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            loop.close()


def _quiet_about(abandoned: set[asyncio.Task[Any]]) -> Callable[..., None]:
    """An exception handler for the loop that says nothing more about the tasks that
    were left behind (the line above names them already). Each other report goes to
    the default handler."""

    def handle(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if context.get("task") in abandoned:
            return
        loop.default_exception_handler(context)

    return handle


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


class _SupervisedServer(GracefulServer):
    """Uvicorn's server, but a stop signal goes to the supervisor. The server serves
    on while the supervisor waits for its routines, so they can still report. It
    stops when the supervisor's `run` ends. The signal is not recorded, so uvicorn
    does not raise it again at the end."""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.supervisor: Supervisor | None = None

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if self.supervisor is None:
            super().handle_exit(sig, frame)  # GracefulServer: graceful first, then at once
        else:
            self.supervisor.on_signal()

    def stop_all(self) -> None:
        """Stop the server, and stop the supervisor at once: its control plane does no
        more work. It runs on the server's loop (`Serving.on_lost`): it does not block
        and does not raise."""
        self.should_exit = True
        if self.supervisor is not None:
            self.supervisor.on_signal()
            self.supervisor.on_signal()


def _serve_with_supervisor(serving: Serving, folder: Folder) -> int:
    """Run `_serve_and_supervise`. Returns the exit code."""
    try:
        return _run_bounded(_serve_and_supervise(serving, folder))
    except SupervisorError as error:
        _exit_with(str(error))
    except KeyboardInterrupt:  # a signal before the server took the signals
        return 1


async def _serve_and_supervise(serving: Serving, folder: Folder) -> int:
    """The control plane and the supervisor of `folder` in one loop. The supervisor
    speaks HTTP to the server's own address. Returns the supervisor's exit code:
    1 also when the server stopped before the supervisor ran (a lost hold, or a
    signal at the start)."""
    server = _SupervisedServer(serving.config)
    serving.on_lost(server.stop_all)
    serving_task = asyncio.create_task(server.serve(sockets=[serving.listener]))
    api = ControlPlaneApi(serving.url)
    supervising = asyncio.create_task(_supervise_beside(server, folder, api))
    try:
        await asyncio.wait({serving_task, supervising}, return_when=asyncio.FIRST_COMPLETED)
        if not supervising.done():  # the server stopped first
            if server.supervisor is None:
                supervising.cancel()
            else:
                server.stop_all()
        await asyncio.wait({supervising})
        code = 1 if supervising.cancelled() else supervising.result()
        if code != 0:
            server.force_exit = True
        return code
    finally:
        await api.aclose()
        server.should_exit = True
        await serving_task


async def _supervise_beside(server: _SupervisedServer, folder: Folder, api: ControlPlaneApi) -> int:
    """Wait until the server serves, then open and run the supervisor of `folder`."""
    while not server.started:
        await asyncio.sleep(0.05)
    supervisor = await open_supervisor(folder, api)
    server.supervisor = supervisor
    return await supervisor.run()


app.add_typer(token_app, name="token")
