"""The pyproject.toml that `stateous init` writes (see the docs,
Reference > The files): the supervisor's folder is a Python project of its own, so `uv sync`
makes its `.venv`."""

import tomllib
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest

from stateous.cli.errors import CommandError
from stateous.cli.project_file import checkout, write_pyproject

CHECKOUT = Path(__file__).resolve().parents[3]
"""This repository: a Stateous checkout."""


def read(folder: Path) -> dict[str, Any]:
    return tomllib.loads((folder / "pyproject.toml").read_text(encoding="utf-8"))


def test_it_depends_on_the_stateous_that_wrote_it(tmp_path: Path) -> None:
    folder = tmp_path / "sdlc-supervisor"

    said = write_pyproject(folder)

    data = read(folder)
    assert data["project"] == {
        "name": "sdlc-supervisor",
        "version": "0.1.0",
        "requires-python": ">=3.12",
        "dependencies": [f"stateous>={version('stateous')}"],
    }
    assert "tool" not in data
    assert said == f"wrote    {folder / 'pyproject.toml'}"


def test_the_header_tells_what_the_file_is_for(tmp_path: Path) -> None:
    write_pyproject(tmp_path)

    text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    header = text.split("[project]")[0]
    assert "uv sync" in header
    assert ".venv" in header
    assert "dependencies" in header


def test_a_local_checkout_becomes_an_editable_source(tmp_path: Path) -> None:
    write_pyproject(tmp_path, stateous_path=checkout(CHECKOUT))

    text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    source = read(tmp_path)["tool"]["uv"]["sources"]["stateous"]
    assert source == {"path": CHECKOUT.as_posix(), "editable": True}
    assert "# A local checkout of Stateous: delete this table to use Stateous from PyPI." in text


def test_a_checkout_is_given_as_an_absolute_path_with_forward_slashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(CHECKOUT / "src")

    assert checkout(Path("..")) == CHECKOUT.as_posix()


def test_a_path_that_is_not_a_stateous_checkout_is_refused(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "pyproject.toml").write_text('[project]\nname = "other"\n', encoding="utf-8")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    a_file = tmp_path / "a-file"
    a_file.write_text("", encoding="utf-8")

    for path in (other, broken, a_file, tmp_path / "missing", tmp_path):
        with pytest.raises(CommandError, match="is not a Stateous checkout") as refused:
            checkout(path)
        assert "\n" not in str(refused.value)


def test_an_existing_pyproject_is_kept(tmp_path: Path) -> None:
    mine = '[project]\nname = "mine"\n'
    (tmp_path / "pyproject.toml").write_text(mine, encoding="utf-8")

    said = write_pyproject(tmp_path, stateous_path=checkout(CHECKOUT))

    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == mine
    assert said == f"kept     {tmp_path / 'pyproject.toml'}"


@pytest.mark.parametrize(
    ("folder", "name"),
    [
        ("acme-sup", "acme-sup"),
        ("gpu_box", "gpu_box"),
        ("box-", "box"),
        ("a.b.", "a.b"),
        ("my box (2)", "my-box--2"),
        ("___", "supervisor"),
    ],
)
def test_the_project_is_named_after_its_folder_as_packaging_allows(
    tmp_path: Path, folder: str, name: str
) -> None:
    write_pyproject(tmp_path / folder)

    assert read(tmp_path / folder)["project"]["name"] == name


def test_the_current_folder_is_named_after_its_real_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "box").mkdir()
    monkeypatch.chdir(tmp_path / "box")

    write_pyproject(Path("."))

    assert read(tmp_path / "box")["project"]["name"] == "box"


def test_it_is_written_with_unix_line_endings(tmp_path: Path) -> None:
    write_pyproject(tmp_path, stateous_path=checkout(CHECKOUT))

    assert b"\r\n" not in (tmp_path / "pyproject.toml").read_bytes()
