"""`serve --supervise`: where a stop signal goes, before and after the supervisor
ran, and how a lost hold stops the supervisor."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from starlette.applications import Starlette

from stateous.cli import main
from stateous.supervisor.folder import Folder, FolderConfig


class FakeSupervisor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def on_signal(self) -> None:
        self.calls.append("signal")

    def stop_now(self) -> None:
        self.calls.append("stop_now")

    async def run(self) -> int:
        return 0


def server() -> main._SupervisedServer:  # pyright: ignore[reportPrivateUsage]
    return main._SupervisedServer(uvicorn.Config(Starlette()))  # pyright: ignore[reportPrivateUsage]


def test_a_signal_goes_to_the_supervisor_while_it_runs() -> None:
    supervised = server()
    fake = FakeSupervisor()
    supervised.supervisor = fake  # pyright: ignore[reportAttributeAccessIssue]

    supervised.handle_exit(signal.SIGINT, None)

    assert fake.calls == ["signal"]
    assert not supervised.should_exit


def test_a_lost_hold_stops_the_supervisor_at_once_with_stop_now() -> None:
    supervised = server()
    fake = FakeSupervisor()
    supervised.supervisor = fake  # pyright: ignore[reportAttributeAccessIssue]

    supervised.stop_all()

    assert fake.calls == ["stop_now"]
    assert supervised.should_exit


def test_after_the_supervisor_ran_signals_stop_the_server_and_a_second_forces_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeSupervisor()

    async def open_fake(folder: Folder, api: Any) -> FakeSupervisor:
        return fake

    monkeypatch.setattr(main, "open_supervisor", open_fake)
    supervised = server()
    supervised.started = True
    folder = Folder(tmp_path, FolderConfig(url="http://cp.test"), "sha256:x")

    code = asyncio.run(main._supervise_beside(supervised, folder, None))  # pyright: ignore[reportPrivateUsage, reportArgumentType]

    assert code == 0
    supervised.handle_exit(signal.SIGINT, None)
    assert supervised.should_exit and not supervised.force_exit
    supervised.handle_exit(signal.SIGINT, None)
    assert supervised.force_exit
    assert fake.calls == []
