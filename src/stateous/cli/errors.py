"""How a command fails: one plain sentence on stderr, and exit code 1. Never a
traceback."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import ParamSpec, TypeVar

import typer

from stateous.client import ClientError

P = ParamSpec("P")
R = TypeVar("R")


class CommandError(Exception):
    """A command cannot go on. The message is one plain sentence."""


def reported(command: Callable[P, R]) -> Callable[P, R]:
    """Show an expected failure as `error: <sentence>` and exit 1."""

    @functools.wraps(command)
    def run(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return command(*args, **kwargs)
        except (CommandError, ClientError) as error:
            typer.echo(f"error: {error}", err=True)
            raise typer.Exit(1) from None

    return run
