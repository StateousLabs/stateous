"""The exit paths of `serve` and `supervise`, and the branches of
`serve_and_supervise`. The process-exit call and the server are fakes."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
from pathlib import Path
from typing import Any, NoReturn

import pytest
import typer

from stateous.cli import running
from stateous.supervisor.supervisor import SupervisorError


class Exited(Exception):
    """The fake `exit_now` raises it, with the code."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def fake_exit_now(code: int) -> NoReturn:
    raise Exited(code)


def test_exit_now_flushes_then_ends_the_process_with_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_shutdown() -> None:
        calls.append("log")

    monkeypatch.setattr(logging, "shutdown", fake_shutdown)

    def fake_exit(code: int) -> None:
        calls.append(f"exit {code}")

    monkeypatch.setattr(os, "_exit", fake_exit)

    class FakeStdout:
        def flush(self) -> None:
            calls.append("flush")

    monkeypatch.setattr(sys, "stdout", FakeStdout())

    running.exit_now(3)

    assert calls == ["log", "flush", "exit 3"]


def test_supervise_exits_at_once_with_1_after_ctrl_c_before_the_handlers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def interrupted(folder: Path) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(running, "load_folder", interrupted)
    monkeypatch.setattr(running, "exit_now", fake_exit_now)

    with pytest.raises(Exited) as raised:
        running.supervise(tmp_path)

    assert raised.value.code == 1


def test_the_loop_hides_the_report_of_an_abandoned_task_only() -> None:
    reports: list[dict[str, Any]] = []

    class FakeLoop:
        def default_exception_handler(self, context: dict[str, Any]) -> None:
            reports.append(context)

    abandoned: Any = object()
    handler = running._quiet_about({abandoned})
    loop: Any = FakeLoop()

    handler(loop, {"task": abandoned, "message": "hidden"})
    handler(loop, {"task": object(), "message": "shown"})

    assert [report["message"] for report in reports] == ["shown"]


class FakeClock:
    lost = False


class FakeServing:
    url = "http://127.0.0.1:1"
    clock = FakeClock()

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_serve_with_a_supervisor_exits_at_once_when_the_supervisor_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    serving = FakeServing()

    def load(folder: Path) -> object:
        return object()

    def prepare(graph: Path, db: str, listen: str) -> FakeServing:
        return serving

    def failed(serving: object, folder: object) -> tuple[int, bool]:
        return 1, True

    monkeypatch.setattr(running, "load_folder", load)
    monkeypatch.setattr(running, "prepare_serve", prepare)
    monkeypatch.setattr(running, "serve_with_supervisor", failed)
    monkeypatch.setattr(running, "exit_now", fake_exit_now)

    with pytest.raises(Exited) as raised:
        running.serve(tmp_path / "g.json", "sqlite://", "127.0.0.1:0", tmp_path)

    assert raised.value.code == 1
    assert serving.closed


def test_a_supervisor_error_in_the_loop_is_one_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(main: object) -> tuple[int, bool]:
        raise SupervisorError("the routine file is broken")

    monkeypatch.setattr(running, "run_bounded", broken)
    monkeypatch.setattr(running, "serve_and_supervise", no_coroutine)

    with pytest.raises(typer.Exit) as raised:
        running.serve_with_supervisor(FakeServing(), object())  # type: ignore[arg-type]

    assert raised.value.exit_code == 1
    assert capsys.readouterr().err == "error: the routine file is broken\n"


def test_ctrl_c_before_the_server_took_the_signals_gives_code_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted(main: object) -> tuple[int, bool]:
        raise KeyboardInterrupt

    monkeypatch.setattr(running, "run_bounded", interrupted)
    monkeypatch.setattr(running, "serve_and_supervise", no_coroutine)

    result = running.serve_with_supervisor(FakeServing(), object())  # type: ignore[arg-type]

    assert result == (1, True)


def no_coroutine(serving: object, folder: object) -> None:
    """Stands for `serve_and_supervise`: `run_bounded` is a fake, so nothing awaits it."""


class FakeServer:
    """A server that serves until `should_exit`. `serve_at_once` makes it stop first."""

    serve_at_once = False

    def __init__(self, config: object) -> None:
        self.started = True
        self.supervisor: object | None = None
        self.should_exit = False
        self.force_exit = False
        self.stopped_all = asyncio.Event()

    def stop_all(self) -> None:
        self.stopped_all.set()

    async def serve(self, sockets: list[socket.socket]) -> None:
        while not (self.should_exit or self.serve_at_once):
            await asyncio.sleep(0)


class FakeApi:
    def __init__(self, url: str) -> None:
        self.url = url

    async def aclose(self) -> None:
        return None


class FakeServingSockets(FakeServing):
    config = object()
    listener: Any = None

    def on_lost(self, callback: object) -> None:
        return None


def run_serve_and_supervise(
    monkeypatch: pytest.MonkeyPatch, beside: Any, *, server_first: bool
) -> tuple[int, FakeServer]:
    """Run `serve_and_supervise` with a fake server. `beside` is `supervise_beside`."""
    made: list[FakeServer] = []

    class Server(FakeServer):
        serve_at_once = server_first

        def __init__(self, config: object) -> None:
            super().__init__(config)
            made.append(self)

    monkeypatch.setattr(running, "SupervisedServer", Server)
    monkeypatch.setattr(running, "ControlPlaneApi", FakeApi)
    monkeypatch.setattr(running, "supervise_beside", beside)
    serving: Any = FakeServingSockets()
    folder: Any = object()
    code = asyncio.run(running.serve_and_supervise(serving, folder))
    return code, made[0]


def test_a_failed_supervisor_forces_the_server_to_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    async def beside(server: FakeServer, folder: object, api: object) -> int:
        return 3

    code, server = run_serve_and_supervise(monkeypatch, beside, server_first=False)

    assert code == 3
    assert server.force_exit


def test_the_server_that_stops_first_cancels_a_supervisor_that_did_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def beside(server: FakeServer, folder: object, api: object) -> int:
        await asyncio.Event().wait()  # never ends: only the cancellation ends it
        return 0

    code, server = run_serve_and_supervise(monkeypatch, beside, server_first=True)

    assert code == 1
    assert server.force_exit


def test_the_server_that_stops_first_stops_a_supervisor_that_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def beside(server: FakeServer, folder: object, api: object) -> int:
        server.supervisor = object()
        await server.stopped_all.wait()
        return 0

    code, server = run_serve_and_supervise(monkeypatch, beside, server_first=True)

    assert code == 0
    assert server.stopped_all.is_set()
