"""The sentences of `compile`, `init` and the closed output when a file cannot be
written, read or used."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import io
import os
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

import stateous.cli.project_file as project_file
from stateous import END, START, Workflow
from stateous.cli.main import _Stdout, app, main
from stateous.cli.tokens import _local
from stateous.compiler.compile import compile_workflow
from stateous.model.graph_file import dump_graph

ROOT = Path(__file__).parents[3]
FLOW = ROOT / "examples" / "sdlc" / "flow.py"

runner = CliRunner()


@pytest.fixture
def in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def graph_file(folder: Path) -> Path:
    """Compile the sdlc example to a graph file in `folder`."""
    target = folder / "sdlc.json"
    result = runner.invoke(app, ["compile", str(FLOW), "--out", str(target)])
    assert result.exit_code == 0, result.output
    return target


def test_compile_names_the_target_when_it_cannot_write(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder", encoding="utf-8")
    target = blocker / "sdlc.json"

    result = runner.invoke(app, ["compile", str(FLOW), "--out", str(target)])

    assert result.exit_code == 1
    assert result.output.startswith(f"error: cannot write {target}: ")


def test_init_workflow_keeps_a_schemas_file_that_exists(in_tmp: Path) -> None:
    folder = in_tmp / "orders"
    folder.mkdir()
    schemas = folder / "schemas.py"
    schemas.write_text("# my own models\n", encoding="utf-8")

    result = runner.invoke(app, ["init", "workflow", "orders"])

    assert f"kept     {Path('orders') / 'schemas.py'}" in result.output
    assert schemas.read_text(encoding="utf-8") == "# my own models\n"


def test_init_workflow_names_the_folder_when_it_cannot_write(in_tmp: Path) -> None:
    (in_tmp / "orders").write_text("a file, not a folder", encoding="utf-8")

    result = runner.invoke(app, ["init", "workflow", "orders"])

    assert result.exit_code == 1
    assert result.output.startswith("error: cannot write orders: ")


def test_init_supervisor_names_the_folder_when_it_cannot_write(in_tmp: Path) -> None:
    graph = graph_file(in_tmp)
    (in_tmp / "sup").write_text("a file, not a folder", encoding="utf-8")

    result = runner.invoke(app, ["init", "supervisor", str(graph), "sup"])

    assert result.exit_code == 1
    assert result.output.startswith("error: cannot write sup: ")


def test_init_supervisor_says_when_no_contract_can_be_written(in_tmp: Path) -> None:
    class Ticket(BaseModel):
        repo: str

    class TicketContext(BaseModel):  # clashes with the alias for the context of Ticket
        note: str

    flow = Workflow("w", context=Ticket)
    a = flow.state("a")
    flow.transition(START, a)
    flow.transition(a, END, on=flow.event("done", payload=TicketContext))
    graph = in_tmp / "w.json"
    graph.write_text(dump_graph(compile_workflow(flow)), encoding="utf-8")

    result = runner.invoke(app, ["init", "supervisor", str(graph), "sup"])

    assert result.exit_code == 1
    prefix = f"error: cannot write a contract for the workflow of {graph}: "
    assert result.output.startswith(prefix + "a model is named 'TicketContext'")


def test_init_supervisor_refuses_a_source_that_is_not_a_graph_or_a_flow(in_tmp: Path) -> None:
    other = in_tmp / "notes.txt"
    other.write_text("hello", encoding="utf-8")

    result = runner.invoke(app, ["init", "supervisor", str(other), "sup"])

    assert result.exit_code == 1
    assert result.output == f"error: {other} is not a graph file (.json) or a flow.py\n"


def test_init_supervisor_says_when_the_graph_file_cannot_be_read(in_tmp: Path) -> None:
    graph = in_tmp / "bad.json"
    graph.write_bytes(b"\xff\xfe\x00 not utf-8 \xff")

    result = runner.invoke(app, ["init", "supervisor", str(graph), "sup"])

    assert result.exit_code == 1
    assert result.output.startswith(f"error: cannot read {graph}: ")


def test_requirement_is_the_bare_name_when_the_version_is_not_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unknown(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(project_file, "version", unknown)

    assert project_file.requirement() == project_file.PACKAGE


def test_a_text_that_is_not_a_time_shows_as_it_is() -> None:
    assert _local("not a time") == "not a time"


class FlushBroken(io.StringIO):
    """Stdout whose flush fails with a closed pipe. It has a file descriptor."""

    def __init__(self, fd: int) -> None:
        super().__init__()
        self.fd = fd

    def fileno(self) -> int:
        return self.fd

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


def test_a_closed_pipe_at_the_final_flush_ends_the_command_with_code_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fd = os.open(tmp_path / "stdout", os.O_WRONLY | os.O_CREAT)
    try:
        monkeypatch.setattr(sys, "stdout", FlushBroken(fd))
        monkeypatch.setattr(sys, "argv", ["stateous", "--help"])

        with pytest.raises(SystemExit) as raised:
            main()
    finally:
        os.close(fd)

    assert raised.value.code == 1


def test_writelines_goes_to_the_stream() -> None:
    stream = io.StringIO()

    _Stdout(stream).writelines(["a", "b"])  # type: ignore[arg-type]

    assert stream.getvalue() == "ab"


def test_a_failed_writelines_is_kept_and_raised() -> None:
    class Failing(io.StringIO):
        def writelines(self, lines: object) -> None:
            raise OSError(22, "Invalid argument")

    wrapper = _Stdout(Failing())  # type: ignore[arg-type]

    with pytest.raises(OSError):
        wrapper.writelines(["a"])

    assert wrapper.failure is not None
    assert wrapper.failure.errno == 22
