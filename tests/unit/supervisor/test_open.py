"""Starting a supervisor: the checks against the control plane, and the signals."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import pytest

from stateous.supervisor.folder import Folder, FolderConfig
from stateous.supervisor.outbox import Outbox, outbox_path
from stateous.supervisor.supervisor import (
    Supervisor,
    SupervisorError,
    open_supervisor,
    stop_on_signals,
)

from .fakes import FakeApi, grant, make

CHECKSUM = "sha256:" + "b" * 64

ROUTINE = (
    "from stateous import StateRef, on_state\n\n"
    "CODING = StateRef('coding')\n\n\n"
    "@on_state(CODING)\ndef coding(ctx: object) -> None:\n    return None\n"
)


def folder_at(path: Path, *, routine: str | None = ROUTINE) -> Folder:
    path.mkdir(exist_ok=True)
    if routine is not None:
        (path / "coding.py").write_text(routine, encoding="utf-8")
    config = FolderConfig(url="http://fake.test", tasks=2, name="box-1")
    return Folder(path.resolve(), config, CHECKSUM)


def test_a_folder_that_matches_the_control_plane_gives_a_supervisor(tmp_path: Path) -> None:
    folder = folder_at(tmp_path / "w1")

    supervisor = asyncio.run(open_supervisor(folder, FakeApi(checksum=CHECKSUM)))
    supervisor.close()

    assert (supervisor.name, supervisor.tasks) == ("box-1", 2)
    assert supervisor.registry.served == ["coding"]
    assert supervisor.heartbeat_every == 30.0


def test_the_supervisor_makes_its_outbox_in_the_folder(tmp_path: Path) -> None:
    folder = folder_at(tmp_path / "w5")

    supervisor = asyncio.run(open_supervisor(folder, FakeApi(checksum=CHECKSUM)))
    try:
        assert (folder.path / ".stateous" / "outbox.db").is_file()
        supervisor.outbox.append(1, 1, "T-1", "note", {"text": "one"})
    finally:
        supervisor.close()

    with Outbox.open(outbox_path(folder.path)) as outbox:
        assert [message.body for message in outbox.pending()] == [{"text": "one"}]


def test_a_second_supervisor_on_the_same_folder_is_refused_in_one_sentence(
    tmp_path: Path,
) -> None:
    folder = folder_at(tmp_path / "w6")
    first = asyncio.run(open_supervisor(folder, FakeApi(checksum=CHECKSUM)))
    try:
        with pytest.raises(SupervisorError) as refused:
            asyncio.run(open_supervisor(folder, FakeApi(checksum=CHECKSUM)))
    finally:
        first.close()

    assert str(refused.value) == (
        f"another supervisor runs on {folder.path} now: stop it, or use another folder"
    )
    second = asyncio.run(open_supervisor(folder, FakeApi(checksum=CHECKSUM)))
    second.close()  # the first one stopped: the folder is free


def test_a_refused_start_releases_the_outbox(tmp_path: Path) -> None:
    folder = folder_at(tmp_path / "w7", routine=None)

    with pytest.raises(SupervisorError, match="has no routine"):
        asyncio.run(open_supervisor(folder, FakeApi(checksum=CHECKSUM)))

    with Outbox.open(outbox_path(folder.path)) as outbox:
        assert outbox.pending() == []


def test_a_contract_for_another_graph_is_refused(tmp_path: Path) -> None:
    folder = folder_at(tmp_path / "w2")

    with pytest.raises(SupervisorError) as refused:
        asyncio.run(open_supervisor(folder, FakeApi(checksum="sha256:" + "c" * 64)))

    message = str(refused.value)
    assert "contract.py is for another version of the workflow" in message
    assert f"run `stateous init http://fake.test {folder.path}` again" in message


def test_a_folder_without_routines_is_refused(tmp_path: Path) -> None:
    folder = folder_at(tmp_path / "w3", routine=None)

    with pytest.raises(SupervisorError, match=r"has no routine: mark a function with @on_state"):
        asyncio.run(open_supervisor(folder, FakeApi(checksum=CHECKSUM)))


def test_a_routine_that_does_not_import_is_refused_in_one_sentence(tmp_path: Path) -> None:
    folder = folder_at(tmp_path / "w4", routine="import not_a_module_anywhere\n")

    with pytest.raises(SupervisorError) as refused:
        asyncio.run(open_supervisor(folder, FakeApi(checksum=CHECKSUM)))

    assert str(refused.value).startswith(f"cannot load the routines in {folder.path}: ")
    assert "ModuleNotFoundError" in str(refused.value)


def test_ctrl_c_goes_to_the_supervisor_and_the_old_handlers_come_back() -> None:
    api = FakeApi(grant(1, "coding"))
    supervisor: Supervisor = make(api)
    before = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }

    with stop_on_signals(supervisor):
        if sys.platform == "win32":
            assert signal.getsignal(signal.SIGBREAK) is not signal.SIG_DFL
        signal.raise_signal(signal.SIGINT)  # Ctrl-C, also on Windows

    assert {s: signal.getsignal(s) for s in before} == before
    assert asyncio.run(supervisor.run()) == 0  # the signal came before run: it stops at once
    assert api.claims() == 0
