"""The long commands: `stateous serve` and `stateous supervise`.

This module imports the server, the database and the supervisor. `stateous.cli.main`
imports it only in these two commands, so `--help`, `token ...`, `compile` and
`init` start fast."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import FrameType
from typing import Any, NoReturn

import typer
import uvicorn

from stateous.cli.errors import exit_with
from stateous.cli.serving import (
    GracefulServer,
    ServeRefused,
    Serving,
    configure_logging,
    prepare_serve,
)
from stateous.supervisor.folder import Folder, FolderError, load_folder
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
(for example a routine that ignores its cancellation), and then for the threads of
the loop (`asyncio.to_thread`). Then the command exits."""


def serve(graph: Path, db: str, listen: str, supervise_folder: Path | None) -> None:
    """The body of `stateous serve`."""
    configure_logging()
    folder: Folder | None = None
    if supervise_folder is not None:
        try:
            folder = load_folder(supervise_folder)
        except FolderError as error:
            exit_with(str(error))
    try:
        serving = prepare_serve(graph, db, listen)
    except ServeRefused as error:
        exit_with(str(error))
    typer.echo(f"serving on {serving.url} (Ctrl-C stops)", err=True)
    code, clean = 0, True
    try:
        if folder is None:
            server = GracefulServer(serving.config)

            def stop() -> None:
                server.should_exit = True

            serving.on_lost(stop)
            server.run(sockets=[serving.listener])
        else:
            log_to_stderr()
            code, clean = serve_with_supervisor(serving, folder)
    finally:
        serving.close()
    if serving.clock.lost:
        typer.echo(
            "error: another control plane took the database; this control plane stopped",
            err=True,
        )
        code = 1
    if code != 0 or not clean:
        if folder is not None:
            exit_now(code)
        raise typer.Exit(code)
    typer.echo("stopped", err=True)


def supervise(folder: Path) -> None:
    """The body of `stateous supervise`."""
    log_to_stderr()
    try:
        loaded = load_folder(folder)
        code, clean = run_bounded(supervise_folder(loaded))
    except (FolderError, SupervisorError) as error:
        exit_with(str(error))
    except KeyboardInterrupt:  # Ctrl-C before the handlers were in place
        code, clean = 1, True
    if code != 0 or not clean:
        exit_now(code)
    raise typer.Exit(code)


def log_to_stderr() -> None:
    """The supervisor's log lines on stderr, at INFO. `configure_logging` adds a
    handler only when there is none, so a second call adds no second handler. The
    HTTP libraries log only warnings."""
    configure_logging()
    for name in ("httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def run_bounded(
    main: Coroutine[Any, Any, int], *, leftover_wait: float = LEFTOVER_WAIT
) -> tuple[int, bool]:
    """Run `main` on a new event loop. Returns its exit code, and whether the end is
    clean. As `asyncio.run`, but the end does not wait without limit: the tasks
    left on the loop are cancelled, and the loop waits at most `leftover_wait`
    seconds for them. So a routine that ignores its cancellation cannot stop the
    command from exiting. Then it waits at most `leftover_wait` seconds for the
    threads of the loop (`asyncio.to_thread`). When one still runs, the end is not
    clean: a normal exit would wait for that thread, so only `exit_now` ends the
    process."""
    loop = asyncio.new_event_loop()
    executor = ThreadPoolExecutor(thread_name_prefix="stateous-to-thread")
    loop.set_default_executor(executor)
    clean = False
    try:
        code = loop.run_until_complete(main)
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
            clean = _threads_ended(executor, leftover_wait)
        finally:
            loop.close()
    return code, clean


def _threads_ended(executor: ThreadPoolExecutor, wait: float) -> bool:
    """Shut down `executor`. True when its threads end within `wait` seconds."""
    ended = threading.Event()

    def shut_down() -> None:
        executor.shutdown(wait=True, cancel_futures=True)
        ended.set()

    threading.Thread(target=shut_down, name="stateous-shutdown", daemon=True).start()
    if ended.wait(wait):
        return True
    log.warning("a thread of a routine did not stop; exiting without it")
    return False


def exit_now(code: int) -> NoReturn:
    """End the process with `code` at once, after the log and the output are
    flushed. A routine's thread that still runs (`asyncio.to_thread`) cannot hold
    the exit then. Use it after a forced stop, when the loop is closed."""
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
    os._exit(code)


def _quiet_about(abandoned: set[asyncio.Task[Any]]) -> Callable[..., None]:
    """An exception handler for the loop that says nothing more about the tasks that
    were left behind (the line above names them already). Each other report goes to
    the default handler."""

    def handle(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if context.get("task") in abandoned:
            return
        loop.default_exception_handler(context)

    return handle


async def supervise_folder(folder: Folder) -> int:
    """Open and run the supervisor of `folder`. Returns its exit code."""
    api = ControlPlaneApi(folder.config.url)
    try:
        supervisor = await open_supervisor(folder, api)
        try:
            with stop_on_signals(supervisor):
                return await supervisor.run()
        finally:
            supervisor.close()
    finally:
        await api.aclose()


class SupervisedServer(GracefulServer):
    """Uvicorn's server, but a stop signal goes to the supervisor. The server serves
    on while the supervisor waits for its routines, so they can still report. It
    stops when the supervisor's `run` ends. The signal is not recorded, so uvicorn
    does not raise it again at the end."""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.supervisor: Supervisor | None = None

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if self.supervisor is None:  # not yet running, or `run` has returned
            super().handle_exit(sig, frame)  # GracefulServer: graceful first, then at once
        else:
            self.supervisor.on_signal()

    def stop_all(self) -> None:
        """Stop the server, and stop the supervisor at once: its control plane does no
        more work. It runs on the server's loop (`Serving.on_lost`): it does not block
        and does not raise."""
        self.should_exit = True
        if self.supervisor is not None:
            self.supervisor.stop_now()


def serve_with_supervisor(serving: Serving, folder: Folder) -> tuple[int, bool]:
    """Run `serve_and_supervise`. Returns the exit code, and whether the end is
    clean (`run_bounded`)."""
    try:
        return run_bounded(serve_and_supervise(serving, folder))
    except SupervisorError as error:
        exit_with(str(error))
    except KeyboardInterrupt:  # a signal before the server took the signals
        return 1, True


async def serve_and_supervise(serving: Serving, folder: Folder) -> int:
    """The control plane and the supervisor of `folder` in one loop. The supervisor
    speaks HTTP to the server's own address. Returns the supervisor's exit code:
    1 also when the server stopped before the supervisor ran (a lost hold, or a
    signal at the start)."""
    server = SupervisedServer(serving.config)
    serving.on_lost(server.stop_all)
    serving_task = asyncio.create_task(server.serve(sockets=[serving.listener]))
    api = ControlPlaneApi(serving.url)
    supervising = asyncio.create_task(supervise_beside(server, folder, api))
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


async def supervise_beside(server: SupervisedServer, folder: Folder, api: ControlPlaneApi) -> int:
    """Wait until the server serves, then open and run the supervisor of `folder`."""
    while not server.started:
        await asyncio.sleep(0.05)
    supervisor = await open_supervisor(folder, api)
    server.supervisor = supervisor
    try:
        return await supervisor.run()
    finally:
        server.supervisor = None  # later signals stop the server: a hung request can be forced
        supervisor.close()
