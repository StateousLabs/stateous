"""`stateous migrate`, and `stateous serve` with and without `--namespace`."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stateous.cli.main import app
from stateous.cli.serving import ServeRefused, prepare_serve, shown_db
from stateous.control.store import Store
from stateous.control.store.migrations import head

from ..control.support import sdlc, sqlite_url

runner = CliRunner()


def test_migrate_prints_one_line_and_exits_0(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "s.db")

    result = runner.invoke(app, ["migrate", "--db", url])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"migrated {shown_db(url)} to {head()}; notices off\n"


def test_migrate_twice_keeps_the_notices_setting(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "s.db")

    on = runner.invoke(app, ["migrate", "--db", url, "--notices"])
    again = runner.invoke(app, ["migrate", "--db", url])
    off = runner.invoke(app, ["migrate", "--db", url, "--no-notices"])

    assert on.stdout.endswith("; notices on\n")
    assert again.stdout.endswith("; notices on\n")
    assert off.stdout.endswith("; notices off\n")
    store = Store(url, migrate=False)
    try:
        assert store.notices_on is False
    finally:
        store.close()


def test_migrate_refuses_a_namespace_on_sqlite(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "s.db")

    result = runner.invoke(app, ["migrate", "--db", url, "--namespace", "plan"])

    assert result.exit_code == 1
    assert result.stderr == (
        "error: a namespace is for Postgres; SQLite keeps one workflow in each file\n"
    )


def test_migrate_refuses_a_bad_namespace_in_one_line() -> None:
    result = runner.invoke(
        app, ["migrate", "--db", "postgresql://u@127.0.0.1:1/db", "--namespace", "Bad-Name"]
    )

    assert result.exit_code == 1
    assert result.stderr.startswith("error: a namespace is a lower-case letter")
    assert result.stderr.count("\n") == 1


def write_graph(tmp_path: Path) -> Path:
    path = tmp_path / "sdlc.json"
    path.write_text(json.dumps(sdlc()), encoding="utf-8")
    return path


def test_serve_needs_no_new_flag(tmp_path: Path) -> None:
    serving = prepare_serve(write_graph(tmp_path), sqlite_url(tmp_path / "s.db"), "127.0.0.1:0")
    try:
        assert serving.plane.health()["workflow"] == "sdlc"
    finally:
        serving.close()


def test_serve_refuses_a_namespace_on_sqlite(tmp_path: Path) -> None:
    with pytest.raises(ServeRefused, match="a namespace is for Postgres"):
        prepare_serve(
            write_graph(tmp_path),
            sqlite_url(tmp_path / "s.db"),
            "127.0.0.1:0",
            namespace="plan",
        )


def test_serve_has_the_namespace_option() -> None:
    result = runner.invoke(app, ["serve", "--help"])

    assert "--namespace" in result.stdout
