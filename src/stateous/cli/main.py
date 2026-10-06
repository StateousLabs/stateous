"""The `stateous` command. Phase 1 has `compile`; later phases add the others."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from stateous.cli.serving import (
    DEFAULT_DB,
    DEFAULT_LISTEN,
    GracefulServer,
    ServeRefused,
    configure_logging,
    prepare_serve,
)
from stateous.cli.tokens import token_app
from stateous.compiler.codegen import CodegenError, generate_contract
from stateous.compiler.compile import compile_workflow
from stateous.compiler.load import LoadError, load_workflow
from stateous.model.graph_file import dump_graph
from stateous.model.workflow import DefinitionError

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
) -> None:
    """Run the control plane: the HTTP API and the clock, over one database."""
    configure_logging()
    try:
        serving = prepare_serve(graph, db, listen)
    except ServeRefused as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"serving on {serving.url} (Ctrl-C stops)", err=True)
    try:
        GracefulServer(serving.config).run(sockets=[serving.listener])
    finally:
        serving.close()
    typer.echo("stopped", err=True)


app.add_typer(token_app, name="token")
