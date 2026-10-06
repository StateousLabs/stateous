"""A command's error is one plain sentence on stderr, and exit code 1."""

import typer
from typer.testing import CliRunner

from stateous.cli.errors import CommandError, reported
from stateous.client import Unreachable


def failing(error: Exception) -> typer.Typer:
    app = typer.Typer()

    @app.command()
    @reported
    def fail() -> None:
        raise error

    return app


def test_a_command_error_is_one_line_and_exit_1() -> None:
    result = CliRunner().invoke(failing(CommandError("the port is in use")), [])

    assert result.exit_code == 1
    assert result.output == "error: the port is in use\n"


def test_a_client_error_is_reported_the_same_way() -> None:
    error = Unreachable("cannot reach the control plane at http://127.0.0.1:1: refused")

    result = CliRunner().invoke(failing(error), [])

    assert result.exit_code == 1
    assert result.output.startswith("error: cannot reach the control plane")
    assert "Traceback" not in result.output
