"""The `stateous` command: compile, serve, init, supervise and token.

The commands import what they need when they run. `serve` and `supervise` import
the server, the database and the supervisor (`stateous.cli.running`). So `--help`,
`token ...`, `compile` and `init` start fast."""

from __future__ import annotations

import errno
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any, TextIO, cast

import typer

from stateous.cli.errors import exit_with
from stateous.cli.tokens import token_app

DEFAULT_DB = "sqlite:///stateous.db"
DEFAULT_LISTEN = "127.0.0.1:8080"

app = typer.Typer(
    help="A process as a graph of states. A control plane keeps every token durable.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """A process as a graph of states. A control plane keeps every token durable."""


@app.command("compile")
def compile_command(
    flow: Annotated[Path, typer.Argument(help="The workflow's file, e.g. sdlc/flow.py.")],
    out: Annotated[
        Path | None,
        typer.Option(
            "--out", help="The graph file to write. Default: build/<workflow>.json next to FLOW."
        ),
    ] = None,
) -> None:
    """Check a workflow, and write its graph file."""
    from stateous.compiler.codegen import CodegenError, generate_contract
    from stateous.compiler.compile import compile_workflow
    from stateous.compiler.load import LoadError, load_workflow
    from stateous.model.graph_file import dump_graph
    from stateous.model.workflow import DefinitionError

    try:
        workflow = load_workflow(flow)
        document = compile_workflow(workflow)
        generate_contract(document)  # a name that cannot be Python is found now, not at `init`
    except (LoadError, DefinitionError, CodegenError) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1) from None
    target = out if out is not None else flow.parent / "build" / f"{workflow.name}.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dump_graph(document), encoding="utf-8", newline="\n")
    except OSError as error:
        typer.echo(f"error: cannot write {target}: {error.strerror}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"wrote {target}")


@app.command()
def serve(
    graph: Annotated[
        Path,
        typer.Argument(help="The graph file that `stateous compile` wrote.", show_default=False),
    ],
    db: Annotated[str, typer.Option("--db", help="The database URL.")] = DEFAULT_DB,
    listen: Annotated[
        str, typer.Option("--listen", help="HOST:PORT to listen on.")
    ] = DEFAULT_LISTEN,
    supervise_folder: Annotated[
        Path | None,
        typer.Option(
            "--supervise",
            help="Also run the supervisor of this folder, in this process (for development).",
        ),
    ] = None,
) -> None:
    """Run the control plane: the HTTP API and the clock, over one database."""
    from stateous.cli import running

    running.serve(graph, db, listen, supervise_folder)


@app.command()
def init(
    url: Annotated[str, typer.Argument(help="The control plane, e.g. http://127.0.0.1:8080.")],
    folder: Annotated[Path, typer.Argument(help="The supervisor's folder. Made if absent.")],
    stateous_path: Annotated[
        Path | None,
        typer.Option(
            "--stateous-path",
            help="A local Stateous checkout for the folder to depend on, to work on Stateous "
            "itself.",
        ),
    ] = None,
) -> None:
    """Write a supervisor's folder: contract.py, a stub for each state with a routine
    (of each workflow), stateous.yaml, pyproject.toml. Run it again to update
    contract.py."""
    from stateous.cli.errors import CommandError
    from stateous.cli.project_file import checkout, write_pyproject
    from stateous.client.client import Client, ClientError
    from stateous.compiler.codegen import generate_contract, generate_stub, routine_states
    from stateous.supervisor.folder import write_folder

    source: str | None = None
    if stateous_path is not None:
        try:
            source = checkout(stateous_path)
        except CommandError as error:
            exit_with(str(error))
    try:
        with Client(url) as client:
            document = client.workflow()
    except ClientError as error:
        exit_with(f"cannot fetch the workflow from {url}: {error}")

    def stub(state: str) -> str:
        return generate_stub(document, state)

    states = routine_states(document)
    try:
        lines = write_folder(
            folder, url=url, contract=generate_contract(document), states=states, stub=stub
        )
        lines.append(write_pyproject(folder, stateous_path=source))
    except OSError as error:
        exit_with(f"cannot write {folder}: {error.strerror or error}")
    for line in lines:
        typer.echo(line)
    typer.echo(f"\nWrite the routines, then run: stateous supervise {folder}")


@app.command()
def supervise(
    folder: Annotated[Path, typer.Argument(help="The supervisor's folder.")],
) -> None:
    """Run the routines of a supervisor's folder. Ctrl-C once: take no new tasks and
    wait for the running routines. Twice: cancel them and exit."""
    from stateous.cli import running

    running.supervise(folder)


app.add_typer(token_app, name="token")


def main() -> None:
    """The `stateous` command. Output to a closed pipe (`stateous token show | head -1`)
    ends the command quietly, with exit code 1: no traceback. Any other error shows as
    usual, also an EINVAL that did not come from a write to stdout."""
    stdout = _Stdout(sys.stdout)
    sys.stdout = cast("TextIO", stdout)
    try:
        try:
            app(prog_name="stateous")
        finally:
            sys.stdout.flush()
    except OSError as error:
        if not _closed_pipe(error, stdout.failure):
            raise
        # Python flushes stdout again at exit: send it to nothing, so that flush cannot fail.
        nothing = os.open(os.devnull, os.O_WRONLY)
        os.dup2(nothing, sys.stdout.fileno())
        sys.exit(1)
    finally:
        sys.stdout = stdout.stream


class _Stdout:
    """Stdout, which keeps the error of a write or a flush that failed. All else goes
    to the stream."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.failure: OSError | None = None

    def write(self, text: str) -> int:
        try:
            return self.stream.write(text)
        except OSError as error:
            self.failure = error
            raise

    def writelines(self, lines: Iterable[str]) -> None:
        try:
            self.stream.writelines(lines)
        except OSError as error:
            self.failure = error
            raise

    def flush(self) -> None:
        try:
            self.stream.flush()
        except OSError as error:
            self.failure = error
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


def _closed_pipe(error: OSError, stdout_failure: OSError | None) -> bool:
    """True when `error` is a write to a pipe that the reader closed. On Windows, the
    error is EINVAL, not EPIPE; but EINVAL also comes from other calls (for example
    `open('a?b.py')`), so it counts only when a write or a flush of stdout raised it."""
    if isinstance(error, BrokenPipeError) or error.errno == errno.EPIPE:
        return True
    return sys.platform == "win32" and error.errno == errno.EINVAL and error is stdout_failure
