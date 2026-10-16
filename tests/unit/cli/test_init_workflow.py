"""`stateous init workflow NAME [--stateous-path PATH]`: a starter workflow folder that
compiles at once (see the docs, Getting started)."""

import json
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stateous.cli.main import app
from stateous.compiler.compile import compile_workflow
from stateous.compiler.load import load_workflow
from stateous.model.graph_file import dump_graph, load_graph

from ..compiler.test_codegen import pyright_errors

CHECKOUT = Path(__file__).resolve().parents[3]
"""This repository: a Stateous checkout."""

runner = CliRunner()


@pytest.fixture(autouse=True)
def in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def test_it_writes_the_four_outputs(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "workflow", "orders"])

    assert result.exit_code == 0, result.output
    folder = Path("orders")
    for name in ("flow.py", "schemas.py", "pyproject.toml", "build/orders.json"):
        assert (tmp_path / folder / name).is_file(), name
        assert str(folder / name) in result.output, name
    assert "stateous serve orders/build/orders.json" in result.output
    assert "stateous init supervisor orders/flow.py orders-supervisor" in result.output


def test_the_starter_graph_is_what_compile_writes(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "workflow", "orders"])
    graph = tmp_path / "orders" / "build" / "orders.json"
    written = graph.read_text(encoding="utf-8")
    graph.unlink()

    again = runner.invoke(app, ["compile", "orders/flow.py"])

    assert again.exit_code == 0, again.output
    assert graph.read_text(encoding="utf-8") == written
    document = load_graph(written)
    assert document["workflow"] == "orders"
    assert written == dump_graph(compile_workflow(load_workflow(tmp_path / "orders/flow.py")))


def test_the_starter_has_two_states_two_events_and_a_payload(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "workflow", "orders"])

    document = json.loads((tmp_path / "orders/build/orders.json").read_text(encoding="utf-8"))
    assert len(document["states"]) == 2
    assert len(document["events"]) == 2
    assert any("payload" in event for event in document["events"].values())
    assert document["context"] is not None
    flow = (tmp_path / "orders" / "flow.py").read_text(encoding="utf-8")
    assert "from schemas import" in flow
    assert 'Workflow("orders"' in flow


def test_the_project_depends_on_stateous_and_is_named_after_the_folder(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "workflow", "orders"])

    text = (tmp_path / "orders/pyproject.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert text.startswith("# Written by `stateous init workflow`.")
    assert data["project"]["name"] == "orders"
    assert data["project"]["dependencies"][0].startswith("stateous")
    assert "tool" not in data


def test_a_stateous_path_writes_the_uv_source_table(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "workflow", "orders", "--stateous-path", str(CHECKOUT)])

    assert result.exit_code == 0, result.output
    data = tomllib.loads((tmp_path / "orders/pyproject.toml").read_text(encoding="utf-8"))
    source = data["tool"]["uv"]["sources"]["stateous"]
    assert source == {"path": CHECKOUT.as_posix(), "editable": True}


def test_a_bad_stateous_path_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "workflow", "orders", "--stateous-path", str(tmp_path)])

    assert result.exit_code == 1
    assert "is not a Stateous checkout" in result.output
    assert not (tmp_path / "orders").exists()


def test_an_existing_flow_is_refused_and_kept(tmp_path: Path) -> None:
    (tmp_path / "orders").mkdir()
    (tmp_path / "orders" / "flow.py").write_text("# mine\n", encoding="utf-8")

    result = runner.invoke(app, ["init", "workflow", "orders"])

    assert result.exit_code == 1
    assert len(result.output.strip().splitlines()) == 1, result.output
    assert "already has flow.py" in result.output
    assert (tmp_path / "orders" / "flow.py").read_text(encoding="utf-8") == "# mine\n"
    assert not (tmp_path / "orders" / "schemas.py").exists()


@pytest.mark.parametrize("name", ["Orders", "1orders", "my orders", "a/b", "x" * 65])
def test_a_name_that_a_workflow_cannot_have_is_refused(tmp_path: Path, name: str) -> None:
    result = runner.invoke(app, ["init", "workflow", name])

    assert result.exit_code == 1
    assert len(result.output.strip().splitlines()) == 1, result.output
    assert "is not valid" in result.output
    assert "Traceback" not in result.output
    assert list(tmp_path.iterdir()) == []


def test_init_with_no_form_shows_the_help() -> None:
    result = runner.invoke(app, ["init"])

    assert "workflow" in result.output
    assert "supervisor" in result.output


def test_the_old_form_is_gone(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "http://127.0.0.1:8080", "w"])

    assert result.exit_code != 0
    assert not (tmp_path / "w").exists()


def test_the_starter_and_its_supervisor_folder_pass_strict_pyright(tmp_path: Path) -> None:
    made = runner.invoke(app, ["init", "workflow", "orders"])
    assert made.exit_code == 0, made.output

    result = runner.invoke(app, ["init", "supervisor", "orders/flow.py", "orders-supervisor"])

    assert result.exit_code == 0, result.output
    supervisor = tmp_path / "orders-supervisor"
    assert (supervisor / "contract.py").is_file()
    assert pyright_errors(supervisor) == []
    assert pyright_errors(tmp_path / "orders") == []
