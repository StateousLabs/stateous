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
    name, document = _compiled(flow)
    target = out if out is not None else flow.parent / "build" / f"{name}.json"
    _write_graph(target, document)
    typer.echo(f"wrote {target}")


def _compiled(flow: Path) -> tuple[str, dict[str, Any]]:
    """Load and check the workflow in `flow`. Returns its name and its graph document.
    A mistake ends the command with one sentence."""
    from stateous.compiler.codegen import CodegenError, generate_contract
    from stateous.compiler.compile import compile_workflow
    from stateous.compiler.load import LoadError, load_workflow
    from stateous.model.workflow import DefinitionError

    try:
        workflow = load_workflow(flow)
        document = compile_workflow(workflow)
        generate_contract(document)  # a bad Python name is found now, not at `init supervisor`
    except (LoadError, DefinitionError, CodegenError) as error:
        exit_with(str(error))
    return workflow.name, document


def _write_graph(target: Path, document: dict[str, Any]) -> None:
    """Write the graph file. A failure ends the command with one sentence."""
    from stateous.model.graph_file import dump_graph

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dump_graph(document), encoding="utf-8", newline="\n")
    except OSError as error:
        exit_with(f"cannot write {target}: {error.strerror}")


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


init_app = typer.Typer(
    help="Write a new workflow's folder, or a supervisor's folder.",
    no_args_is_help=True,
)

StateousPath = Annotated[
    Path | None,
    typer.Option(
        "--stateous-path",
        help="A local Stateous checkout for the folder to depend on, to work on Stateous itself.",
    ),
]


@init_app.command("workflow")
def init_workflow(
    name: Annotated[
        str, typer.Argument(help="The workflow's name. It is also the name of the folder.")
    ],
    stateous_path: StateousPath = None,
) -> None:
    """Write a starter workflow in the folder NAME, and compile it.

    It writes flow.py, schemas.py and pyproject.toml. Then it compiles flow.py to
    NAME/build/NAME.json. It refuses a folder that has a flow.py."""
    from stateous.cli.project_file import WORKFLOW_HEADER, write_pyproject
    from stateous.cli.starter import FLOW, SCHEMAS, flow_text, schemas_text
    from stateous.model.workflow import DefinitionError, Workflow

    try:
        Workflow(name)  # the rule for a name is the rule of flow.py
    except DefinitionError as error:
        exit_with(str(error))
    folder = Path(name)
    flow = folder / FLOW
    if flow.exists():
        exit_with(f"{folder} already has {FLOW}: give a different name, or edit that file")
    source = _checkout(stateous_path)
    lines: list[str] = []
    try:
        folder.mkdir(parents=True, exist_ok=True)
        flow.write_text(flow_text(name), encoding="utf-8", newline="\n")
        lines.append(f"wrote    {flow}")
        schemas = folder / SCHEMAS
        if schemas.exists():
            lines.append(f"kept     {schemas}")
        else:
            schemas.write_text(schemas_text(name), encoding="utf-8", newline="\n")
            lines.append(f"wrote    {schemas}")
        lines.append(write_pyproject(folder, stateous_path=source, header=WORKFLOW_HEADER))
    except OSError as error:
        exit_with(f"cannot write {folder}: {error.strerror or error}")
    for line in lines:
        typer.echo(line)
    _, document = _compiled(flow)
    graph = folder / "build" / f"{name}.json"
    _write_graph(graph, document)
    typer.echo(f"wrote    {graph}")
    typer.echo("\nNext steps:")
    typer.echo(f"  stateous serve {graph.as_posix()}")
    typer.echo(f"  stateous init supervisor {flow.as_posix()} {name}-supervisor")


@init_app.command("supervisor")
def init_supervisor(
    source: Annotated[
        str,
        typer.Argument(
            help="The workflow: a control plane's URL (http://127.0.0.1:8080), a graph "
            "file (sdlc/build/sdlc.json), or a flow.py.",
            show_default=False,
        ),
    ],
    folder: Annotated[Path, typer.Argument(help="The supervisor's folder. Made if absent.")],
    stateous_path: StateousPath = None,
) -> None:
    """Write a supervisor's folder, from a control plane, a graph file or a flow.py.

    It writes contract.py, a stub for each state with a routine (of each workflow),
    stateous.yaml and pyproject.toml. Run it again to update contract.py. With a
    graph file or a flow.py, stateous.yaml gets http://127.0.0.1:8080."""
    from stateous.cli.project_file import write_pyproject
    from stateous.compiler.codegen import (
        CodegenError,
        generate_contract,
        generate_stub,
        routine_states,
    )
    from stateous.supervisor.folder import write_folder

    checkout = _checkout(stateous_path)
    url, document = _workflow_of(source)

    def stub(state: str) -> str:
        return generate_stub(document, state)

    try:
        contract = generate_contract(document)
        states = routine_states(document)
    except CodegenError as error:
        exit_with(f"cannot write a contract for the workflow of {source}: {error}")
    try:
        lines = write_folder(folder, url=url, contract=contract, states=states, stub=stub)
        lines.append(write_pyproject(folder, stateous_path=checkout))
    except OSError as error:
        exit_with(f"cannot write {folder}: {error.strerror or error}")
    for line in lines:
        typer.echo(line)
    typer.echo(f"\nWrite the routines, then run: stateous supervise {folder}")


def _checkout(stateous_path: Path | None) -> str | None:
    """`--stateous-path` as `checkout` returns it; None when it is not given. A path
    that is not a Stateous checkout ends the command with one sentence."""
    from stateous.cli.errors import CommandError
    from stateous.cli.project_file import checkout

    if stateous_path is None:
        return None
    try:
        return checkout(stateous_path)
    except CommandError as error:
        exit_with(str(error))


def _workflow_of(source: str) -> tuple[str, dict[str, Any]]:
    """The URL for `stateous.yaml`, and the graph document of `source`.

    An http:// or https:// source is a control plane: the document is its
    `GET /workflow`. Else `source` is a local path: a graph file (.json), or a
    flow.py that is compiled in memory. For a local source, the URL is the address
    that `serve` listens on by default. Each document gets the checks that `serve`
    does on a graph file (`load_graph`)."""
    from stateous.client.client import Client, ClientError
    from stateous.model.graph_file import GraphFileError, dump_graph, load_graph

    if source.startswith(("http://", "https://")):
        try:
            with Client(source) as client:
                return source, load_graph(dump_graph(client.workflow()))
        except (ClientError, GraphFileError) as error:
            exit_with(f"cannot fetch the workflow from {source}: {error}")
    local = f"http://{DEFAULT_LISTEN}"
    path = Path(source)
    if not path.exists():
        exit_with(
            f"{source} does not exist: give a control plane's URL (http:// or https://), "
            "a graph file or a flow.py"
        )
    if path.is_file() and path.suffix == ".py":
        return local, _compiled(path)[1]
    if not path.is_file() or path.suffix != ".json":
        exit_with(f"{source} is not a graph file (.json) or a flow.py")
    try:
        return local, load_graph(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        exit_with(f"cannot read {source}: {error}")
    except GraphFileError as error:
        exit_with(f"cannot use {source}: {error}")


app.add_typer(init_app, name="init")


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
