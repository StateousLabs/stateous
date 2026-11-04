"""`main`: the try-out script's command line, its checks before the run, and its exit codes."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

from stateous.skill.install import install_skill
from stateous.tryout import main as main_module
from stateous.tryout.main import main

from .support import job

VERSION = version("stateous")
CONTEXT = '{"title": "first"}'


def args(graph: Path, folder: Path, *more: str) -> list[str]:
    database = str(graph.parent / "try.db")
    return [str(graph), str(folder), "--context", CONTEXT, "--database", database, *more]


def test_the_starter_ends_with_exit_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(args(*job(tmp_path, "return events.done()")), skill_version=VERSION)
    assert code == 0
    assert capsys.readouterr().out.splitlines()[0].endswith("completed")


def test_another_stop_ends_with_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(args(*job(tmp_path, "return None")), skill_version=VERSION)
    assert code == 1
    assert "--fire work=EVENT" in capsys.readouterr().out


def test_a_fire_from_the_command_line(tmp_path: Path) -> None:
    code = main(args(*job(tmp_path, "return None"), "--fire", "work=done"), skill_version=VERSION)
    assert code == 0


def test_a_version_mismatch_stops(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(args(*job(tmp_path, "return events.done()")), skill_version="0.0.1")
    assert code == 1
    error = capsys.readouterr().err
    assert "stateous install skill DIR --force" in error
    assert error.count("\n") == 1
    assert not (tmp_path / "job" / "build" / "try.db").exists()


def test_a_phd_routine_without_phd_stops_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graph, folder = job(tmp_path, "return events.done()")
    (folder / "agent.py").write_text("from phd.run import go\n", encoding="utf-8")
    (folder / "a_draft.py").write_text("def (:\n", encoding="utf-8")  # does not parse

    def no_spec(name: str) -> None:
        return None

    monkeypatch.setattr(main_module, "find_spec", no_spec)
    code = main(args(graph, folder), skill_version=VERSION)
    assert code == 1
    assert 'pip install "stateous[phd]"' in capsys.readouterr().err
    assert not (graph.parent / "try.db").exists()


def test_a_supervisor_for_another_graph_stops_with_one_sentence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    graph, folder = job(tmp_path, "return events.done()")
    other = json.loads(graph.read_text(encoding="utf-8"))
    other["states"]["work"]["timeout"] = "1h"  # another checksum
    other_graph = tmp_path / "other.json"
    other_graph.write_text(json.dumps(other), encoding="utf-8")
    code = main(args(other_graph, folder), skill_version=VERSION)
    assert code == 1
    error = capsys.readouterr().err
    assert error.startswith("error: ")
    assert "another version of the workflow" in error
    assert error.count("\n") == 1


def test_a_context_that_the_workflow_refuses_is_one_sentence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    graph, folder = job(tmp_path, "return events.done()")
    code = main([str(graph), str(folder), "--context", '{"name": "x"}'], skill_version=VERSION)
    assert code == 1
    assert capsys.readouterr().err.startswith("error: ")


def test_a_graph_that_does_not_exist_is_one_sentence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [str(tmp_path / "no.json"), str(tmp_path), "--context", "{}"], skill_version=VERSION
    )
    assert code == 1
    assert capsys.readouterr().err.startswith("error: ")


@pytest.mark.parametrize("bad", ["[1]", "{", "x"])
def test_bad_context_is_wrong_use(tmp_path: Path, bad: str) -> None:
    graph, folder = job(tmp_path, "return events.done()")
    assert main([str(graph), str(folder), "--context", bad], skill_version=VERSION) == 2


def test_a_bad_fire_is_wrong_use(tmp_path: Path) -> None:
    graph, folder = job(tmp_path, "return events.done()")
    assert main(args(graph, folder, "--fire", "work"), skill_version=VERSION) == 2


def test_no_arguments_is_wrong_use() -> None:
    assert main([], skill_version=VERSION) == 2


def test_json_output_is_one_json_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(args(*job(tmp_path, "return events.done()"), "--json"), skill_version=VERSION)
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["stop"]["reason"] == "completed"


def test_the_installed_script_runs(tmp_path: Path) -> None:
    graph, folder = job(tmp_path, "return events.done()")
    script = install_skill(tmp_path / "skills") / "scripts" / "try_workflow.py"
    result = subprocess.run(
        [sys.executable, str(script), *args(graph, folder)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "completed" in result.stdout.splitlines()[0]


def test_the_installed_script_has_help(tmp_path: Path) -> None:
    script = install_skill(tmp_path / "skills") / "scripts" / "try_workflow.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert "--fire" in result.stdout


def test_a_routine_with_import_phd_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, folder = job(tmp_path, "return events.done()")
    (folder / "agent.py").write_text("import json, phd\n", encoding="utf-8")

    def no_spec(name: str) -> None:
        return None

    monkeypatch.setattr(main_module, "find_spec", no_spec)
    assert main(args(graph, folder), skill_version=VERSION) == 1
