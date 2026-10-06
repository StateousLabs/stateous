"""`stateous init` and `stateous supervise`, through the CLI, against a real control plane."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from stateous import Client
from stateous.cli.main import app
from stateous.model.graph_file import checksum
from stateous.supervisor.folder import load_folder

from .supervising import compile_flow, control_plane, database, free_port, init_folder

FLOW = """
from pydantic import BaseModel

from stateous import END, START, Workflow


class Ticket(BaseModel):
    repo: str


flow = Workflow("sdlc", context=Ticket)
coding = flow.state("coding")
in_progress = flow.state("in-progress")
flow.transition(START, coding)
flow.transition(coding, in_progress, on=flow.event("done"))
flow.transition(in_progress, END, on=flow.event("passed"))
"""


def test_init_writes_a_folder_that_matches_the_control_plane(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    folder = tmp_path / "sdlc-worker"

    with control_plane(graph, database(tmp_path)) as url, Client(url) as client:
        result = CliRunner().invoke(app, ["init", url, str(folder)])
        document = client.workflow()

    assert result.exit_code == 0, result.output
    assert "stateous supervise" in result.output
    loaded = load_folder(folder)
    assert loaded.checksum == checksum(document)
    assert loaded.config.url == url
    assert "@on_state(states.coding)" in (folder / "coding.py").read_text(encoding="utf-8")
    assert "@on_state(states.in_progress)" in (folder / "in_progress.py").read_text(
        encoding="utf-8"
    )


def test_init_again_replaces_the_contract_and_keeps_the_routines(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    folder = tmp_path / "sdlc-worker"

    with control_plane(graph, database(tmp_path)) as url:
        init_folder(url, folder)
        (folder / "contract.py").write_text("old\n", encoding="utf-8")
        mine = (folder / "coding.py").read_text(encoding="utf-8") + "# mine\n"
        (folder / "coding.py").write_text(mine, encoding="utf-8")
        result = CliRunner().invoke(app, ["init", url, str(folder)])

    assert result.exit_code == 0, result.output
    assert "__checksum__" in (folder / "contract.py").read_text(encoding="utf-8")
    assert (folder / "coding.py").read_text(encoding="utf-8") == mine


def test_init_refuses_an_unreachable_control_plane_in_one_sentence(tmp_path: Path) -> None:
    url = f"http://127.0.0.1:{free_port()}"

    result = CliRunner().invoke(app, ["init", url, str(tmp_path / "w")])

    assert result.exit_code == 1
    assert f"cannot fetch the workflow from {url}" in result.output
    assert "Traceback" not in result.output
    assert not (tmp_path / "w").exists()


def test_supervise_refuses_a_contract_for_another_graph(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    folder = tmp_path / "sdlc-worker"

    with control_plane(graph, database(tmp_path)) as url:
        init_folder(url, folder)
        contract = folder / "contract.py"
        text = contract.read_text(encoding="utf-8")
        old = load_folder(folder).checksum
        contract.write_text(text.replace(old, "sha256:" + "0" * 64), encoding="utf-8")
        result = CliRunner().invoke(app, ["supervise", str(folder)])

    assert result.exit_code == 1
    assert "is for another version of the workflow" in result.output
    assert "run `stateous init" in result.output
    assert "Traceback" not in result.output


def test_supervise_refuses_a_folder_without_stateous_yaml(tmp_path: Path) -> None:
    folder = tmp_path / "w"
    folder.mkdir()
    (folder / "contract.py").write_text("x = 1\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["supervise", str(folder)])

    assert result.exit_code == 1
    assert "has no stateous.yaml: run `stateous init URL" in result.output
