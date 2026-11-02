"""`stateous serve`: the control plane. It runs the HTTP API and the clock over one
database (see the docs, Reference > The commands).

`prepare_serve` does each check before the server starts, and binds the port. Each
refusal is a `ServeRefused` with one plain sentence. The app in `Serving.config`
runs the clock in its ASGI lifespan: about once a second the clock renews the hold
on the database and calls `engine.tick()`. `GracefulServer` stops gracefully on
Ctrl-C, SIGTERM or Ctrl-Break (Windows): requests in progress finish, and the
command exits 0. A second signal stops it at once. When another control plane takes
the database, the clock calls the callback of `Serving.on_lost`: `serve` then stops,
and exits 1."""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import logging
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

import typer
import uvicorn
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from starlette.types import ASGIApp, Receive, Scope, Send

from stateous.control.api import create_app, refusal
from stateous.control.clock import LOST, Clock
from stateous.control.errors import GraphConflict
from stateous.control.graph import Graph
from stateous.control.plane import ControlPlane
from stateous.control.store import DatabaseHeld, ForeignDatabase
from stateous.model.graph_file import GraphFileError, load_graph

log = logging.getLogger("stateous.serve")

LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost")
"""The hosts that only this computer reaches: no warning."""

SHUTDOWN_WITHIN = 5
"""Seconds a graceful stop waits for the requests in progress."""

_IN_USE: frozenset[int] = frozenset(
    {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)}
)
"""The error numbers of "the address is in use", on POSIX and on Windows."""

_NEAR = {"0.0.0.0": "127.0.0.1", "::": "::1"}
"""For a host that means "every address": the address that this computer uses."""


class ServeRefused(Exception):
    """`serve` cannot start. The message is one plain sentence."""


# ── where to listen ─────────────────────────────────────────────────────


def host_and_port(listen: str) -> tuple[str, int]:
    """`--listen` as the host to bind (an IPv6 address without its brackets;
    127.0.0.1 when empty) and the port, from 0 to 65535. Port 0: the operating
    system picks a free port."""
    host, _, port = listen.rpartition(":")
    usage = (
        f"--listen {listen!r} is not HOST:PORT with a port from 0 to 65535; "
        "write, for example, 127.0.0.1:8080 or [::1]:8080"
    )
    if re.fullmatch(r"[0-9]{1,5}", port) is None or not 0 <= int(port) <= 65535:
        raise ServeRefused(usage)
    if host.startswith("[") and host.endswith("]"):
        try:
            return str(ipaddress.IPv6Address(host[1:-1])), int(port)
        except ValueError:
            raise ServeRefused(usage) from None
    if any(bracket in host for bracket in ":[]"):
        raise ServeRefused(usage)
    return host or "127.0.0.1", int(port)


def listen_warning(host: str) -> str | None:
    """One warning line when other computers can reach the API; else None."""
    if host in LOCAL_HOSTS:
        return None
    return (
        f"warning: other computers can reach {host}, and the API has no authentication; "
        "put a proxy or a VPN in front of it"
    )


def bound(host: str, port: int) -> socket.socket:
    """A socket that listens on `host:port`, bound now, before the database is opened.
    `OSError` if it cannot be bound. It never shares the port: on Windows a second
    socket could otherwise bind it too. `::` also takes IPv4 where the system can."""
    try:
        family = socket.AF_INET6 if ipaddress.ip_address(host).version == 6 else socket.AF_INET
    except ValueError:
        family = socket.AF_INET  # a name: uvicorn would bind it as IPv4 too
    dualstack = host == "::" and socket.has_dualstack_ipv6()
    return socket.create_server((host, port), family=family, dualstack_ipv6=dualstack)


# ── the clock ───────────────────────────────────────────────────────────


class ClockedApp:
    """The API as `serve` runs it. Its ASGI lifespan runs the clock while the server
    serves: the clock starts when the server starts, and the server's stop waits for
    the tick in progress. After a lost hold, each request gets 503, `LOST` and the
    code `not_serving`."""

    def __init__(self, api: ASGIApp, clock: Clock) -> None:
        self.api = api
        self.clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
        elif self.clock.lost and scope["type"] == "http":
            await refusal(503, LOST, "not_serving")(scope, receive, send)
        else:
            await self.api(scope, receive, send)

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        ticking: asyncio.Task[None] | None = None
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                ticking = asyncio.create_task(self.clock.run())
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                self.clock.stop()
                if ticking is not None:
                    await ticking
                await send({"type": "lifespan.shutdown.complete"})
                return


# ── ready to serve ──────────────────────────────────────────────────────


@dataclass
class Serving:
    """A control plane that is ready to serve: its port is bound, and its database is
    held. `config` is uvicorn's configuration; its app runs the clock while it serves.
    `url` is where a client on this computer reaches it (with port 0: the port that
    the listener got). `clock` is the clock that the app runs; `clock.lost` is True
    after another control plane took the database."""

    config: uvicorn.Config
    listener: socket.socket
    url: str
    plane: ControlPlane
    clock: Clock

    def on_lost(self, callback: Callable[[], None]) -> None:
        """Call `callback()` once when another control plane takes the database. The
        clock calls it on the server's event loop, after it logs the line of the lost
        hold, so it must not block: it stops the server (`server.should_exit = True`)
        and what runs beside it. Until the server stops, each request gets 503. Call
        `on_lost` before the server runs; a second call replaces the callback."""
        self.clock.on_lost = callback

    def close(self) -> None:
        """Close the listener and the control plane. Closing the control plane gives up
        the hold. A second call does nothing more."""
        self.listener.close()
        self.plane.close()


def prepare_serve(graph: Path, db: str, listen: str) -> Serving:
    """Do each check before the server starts, in this order: `--listen`, the graph
    file, the port, the database and its hold, the structure on record. Each refusal
    is a `ServeRefused` with one sentence, and leaves no port and no database open.
    Then print the warning line for a wide `--listen`."""
    host, port = host_and_port(listen)
    document = _load(graph)
    listener = _listen(host, port)
    try:
        plane = _open(document, db)
    except BaseException:
        listener.close()
        raise
    try:
        if (warning := listen_warning(host)) is not None:
            typer.echo(warning, err=True)
        app = ClockedApp(create_app(plane), plane.clock)
        config = uvicorn.Config(app, log_level="warning", timeout_graceful_shutdown=SHUTDOWN_WITHIN)
        url = _url(host, listener)
    except BaseException:
        plane.close()
        listener.close()
        raise
    return Serving(config=config, listener=listener, url=url, plane=plane, clock=plane.clock)


class GracefulServer(uvicorn.Server):
    """Uvicorn's server. A first signal stops it gracefully, and a second stops it at
    once. It does not raise the signal again after it stops, so `serve` exits 0."""

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if self.should_exit:
            self.force_exit = True
        else:
            self.should_exit = True


def configure_logging() -> None:
    """Set up logging for `serve`. The `stateous` logger stays at INFO: the
    supervisor reads these lines. Each line goes to stderr, one line each."""
    if not logging.getLogger().handlers:
        logging.basicConfig(format="%(asctime)s %(name)s: %(message)s")
    logging.getLogger("stateous").setLevel(logging.INFO)


def _load(path: Path) -> dict[str, Any]:
    """The graph document in the file at `path`. A file that `load_graph` accepts but
    that `Graph` cannot read is also not a graph file."""
    try:
        document = load_graph(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ServeRefused(
            f"cannot read the graph file {path}: {error.strerror or error}"
        ) from None
    except (GraphFileError, UnicodeDecodeError) as error:
        raise ServeRefused(f"{path} is not a graph file: {_one_line(error)}") from None
    try:
        Graph(document)
    except Exception as error:
        raise ServeRefused(f"{path} is not a graph file: {_one_line(error)}") from None
    return document


def _listen(host: str, port: int) -> socket.socket:
    try:
        return bound(host, port)
    except OSError as error:
        if error.errno in _IN_USE:
            raise ServeRefused(
                f"cannot listen on {host}:{port}: another program already uses that port; "
                "stop it, or choose another port with --listen"
            ) from None
        raise ServeRefused(f"cannot listen on {host}:{port}: {error.strerror or error}") from None


def _open(document: dict[str, Any], db: str) -> ControlPlane:
    """The control plane, with its hold on the database, its tables migrated and its
    graph on record."""
    try:
        return ControlPlane(document, database=db, migrate=True)
    except DatabaseHeld as error:
        raise ServeRefused(_one_line(error)) from None
    except ForeignDatabase as error:
        raise ServeRefused(f"cannot use the database {_shown(db)}: {_one_line(error)}") from None
    except GraphConflict as error:
        raise ServeRefused(_one_line(error)) from None
    except Exception as error:  # a driver, a connection or a migration: one sentence
        raise ServeRefused(f"cannot open the database {_shown(db)}: {_one_line(error)}") from None


def _url(host: str, listener: socket.socket) -> str:
    """Where a client on this computer reaches the listener."""
    port = int(listener.getsockname()[1])
    near = _NEAR.get(host, host)
    shown = f"[{near}]" if ":" in near else near
    return f"http://{shown}:{port}"


def _shown(db: str) -> str:
    """The database URL as a message shows it: without its password."""
    try:
        return make_url(db).render_as_string(hide_password=True)
    except ArgumentError:
        return db


def _one_line(error: BaseException) -> str:
    return " ".join(str(error).split()) or type(error).__name__
