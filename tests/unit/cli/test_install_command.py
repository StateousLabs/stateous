"""`stateous install skill DIRECTORY [--force]`."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from stateous.cli.main import app

runner = CliRunner()


def test_install_skill_prints_one_line(tmp_path: Path) -> None:
    result = runner.invoke(app, ["install", "skill", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == f"wrote    {tmp_path / 'stateous'}"


def test_install_skill_twice_exits_1_with_one_sentence(tmp_path: Path) -> None:
    runner.invoke(app, ["install", "skill", str(tmp_path)])
    result = runner.invoke(app, ["install", "skill", str(tmp_path)])
    assert result.exit_code == 1
    assert "--force" in result.output
    assert result.output.count("\n") == 1


def test_install_skill_with_force_replaces_it(tmp_path: Path) -> None:
    runner.invoke(app, ["install", "skill", str(tmp_path)])
    result = runner.invoke(app, ["install", "skill", str(tmp_path), "--force"])
    assert result.exit_code == 0


def test_a_directory_that_is_a_file_exits_1(tmp_path: Path) -> None:
    target = tmp_path / "a-file"
    target.write_text("x", encoding="utf-8")
    result = runner.invoke(app, ["install", "skill", str(target)])
    assert result.exit_code == 1
    assert result.output.startswith("error: cannot write")
