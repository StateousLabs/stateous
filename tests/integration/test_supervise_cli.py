"""`stateous init` and `stateous supervise`, through the CLI, against a real control plane."""

from __future__ import annotations

import tomllib
from pathlib import Path

from typer.testing import CliRunner

from stateous import Client
from stateous.cli.main import app
from stateous.model.graph_file import checksum
from stateous.supervisor.folder import load_folder

from .supervising import compile_flow, control_plane, database, free_port, init_folder

CHECKOUT = Path(__file__).resolve().parents[2]
"""This repository: a Stateous checkout."""

MINE = '[project]\nname = "mine"\n'
"""A pyproject.toml that the user wrote."""

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
    folder = tmp_path / "sdlc-supervisor"

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
    pyproject = tomllib.loads((folder / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["name"] == "sdlc-supervisor"
    lines = result.output.splitlines()
    assert lines.index(f"wrote    {folder / 'stateous.yaml'}") + 1 == lines.index(
        f"wrote    {folder / 'pyproject.toml'}"
    )


def test_init_with_a_stateous_path_points_the_folder_at_that_checkout(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    folder = tmp_path / "sdlc-supervisor"

    with control_plane(graph, database(tmp_path)) as url:
        result = CliRunner().invoke(
            app, ["init", url, str(folder), "--stateous-path", str(CHECKOUT)]
        )

    assert result.exit_code == 0, result.output
    pyproject = tomllib.loads((folder / "pyproject.toml").read_text(encoding="utf-8"))
    source = pyproject["tool"]["uv"]["sources"]["stateous"]
    assert source == {"path": CHECKOUT.as_posix(), "editable": True}


def test_init_refuses_a_stateous_path_that_is_no_checkout_and_writes_nothing(
    tmp_path: Path,
) -> None:
    graph = compile_flow(tmp_path, FLOW)
    folder = tmp_path / "sdlc-supervisor"

    with control_plane(graph, database(tmp_path)) as url:
        result = CliRunner().invoke(
            app, ["init", url, str(folder), "--stateous-path", str(tmp_path)]
        )

    assert result.exit_code == 1
    assert f"--stateous-path {tmp_path} is not a Stateous checkout" in result.output
    assert len(result.output.strip().splitlines()) == 1
    assert "Traceback" not in result.output
    assert not folder.exists()


def test_init_again_replaces_the_contract_and_keeps_the_routines(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    folder = tmp_path / "sdlc-supervisor"

    with control_plane(graph, database(tmp_path)) as url:
        init_folder(url, folder)
        (folder / "contract.py").write_text("old\n", encoding="utf-8")
        mine = (folder / "coding.py").read_text(encoding="utf-8") + "# mine\n"
        (folder / "coding.py").write_text(mine, encoding="utf-8")
        (folder / "pyproject.toml").write_text(MINE, encoding="utf-8")
        result = CliRunner().invoke(app, ["init", url, str(folder)])

    assert result.exit_code == 0, result.output
    assert "__checksum__" in (folder / "contract.py").read_text(encoding="utf-8")
    assert (folder / "coding.py").read_text(encoding="utf-8") == mine
    pyproject = (folder / "pyproject.toml").read_text(encoding="utf-8")
    assert pyproject == MINE
    assert f"kept     {folder / 'pyproject.toml'}" in result.output


def test_init_refuses_an_unreachable_control_plane_in_one_sentence(tmp_path: Path) -> None:
    url = f"http://127.0.0.1:{free_port()}"

    result = CliRunner().invoke(app, ["init", url, str(tmp_path / "w")])

    assert result.exit_code == 1
    assert f"cannot fetch the workflow from {url}" in result.output
    assert "Traceback" not in result.output
    assert not (tmp_path / "w").exists()


def test_init_refuses_a_url_with_a_bad_host_in_one_sentence(tmp_path: Path) -> None:
    url = "http://my host:8000"

    result = CliRunner().invoke(app, ["init", url, str(tmp_path / "w")])

    assert result.exit_code == 1
    assert "has a host that is not valid" in result.output
    assert "Traceback" not in result.output
    assert not (tmp_path / "w").exists()


def test_supervise_refuses_a_bad_host_without_retrying(tmp_path: Path) -> None:
    folder = tmp_path / "w"
    folder.mkdir()
    checksum_line = "sha256:" + "a" * 64
    (folder / "contract.py").write_text(f'__checksum__ = "{checksum_line}"\n', encoding="utf-8")
    (folder / "stateous.yaml").write_text("url: http://my host:8000\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["supervise", str(folder)])

    assert result.exit_code == 1
    assert "has a host that is not valid" in result.output
    assert "Traceback" not in result.output


def test_supervise_refuses_a_contract_for_another_graph(tmp_path: Path) -> None:
    graph = compile_flow(tmp_path, FLOW)
    folder = tmp_path / "sdlc-supervisor"

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
