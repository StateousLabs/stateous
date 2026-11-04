"""The command line of the skill's try-out script, `scripts/try_workflow.py`.

Exit code 0 when the top token completed, 1 when the try-out stopped for another
reason or could not run (one sentence on stderr), and 2 for wrong use.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import sys
from collections.abc import Sequence
from importlib.metadata import version
from importlib.util import find_spec
from pathlib import Path
from typing import Any, NoReturn, cast

from stateous.control.errors import ControlPlaneError
from stateous.control.store.errors import DatabaseHeld, ForeignDatabase, SchemaNotReady
from stateous.model.graph_file import GraphFileError
from stateous.supervisor.supervisor import SupervisorError
from stateous.tryout.run import Fire, parse_fire, try_workflow

DESCRIPTION = """\
Try out a Stateous workflow in one process, with the real routines. It makes a new
SQLite database, starts a control plane, the supervisor of SUPERVISOR_FOLDER and one
token, and stops by itself. Then it prints a report: why it stopped, each task, and
the history of each token. Exit code 0: the token completed. 1: it stopped for
another reason. 2: wrong use."""

FIRE_HELP = """\
An answer for a state that waits: when a token waits at STATE, fire EVENT on it, with
the payload if given. Give it once for each answer. Ask the developer for the answer."""


class WrongUse(Exception):
    """The command line is wrong. The message is one sentence."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise WrongUse(message)


def parser() -> argparse.ArgumentParser:
    found = _Parser(prog="try_workflow.py", description=DESCRIPTION)
    found.add_argument("graph", metavar="GRAPH", type=Path, help="the graph file (.json)")
    found.add_argument(
        "folder", metavar="SUPERVISOR_FOLDER", type=Path, help="the supervisor's folder"
    )
    found.add_argument(
        "--context", required=True, metavar="JSON", help="the context of the token, a JSON object"
    )
    found.add_argument(
        "--fire",
        action="append",
        default=[],
        metavar="STATE=EVENT[:PAYLOAD_JSON]",
        help=FIRE_HELP,
    )
    found.add_argument(
        "--timeout", type=float, default=600, help="stop after this many seconds (default 600)"
    )
    found.add_argument(
        "--quiet",
        type=float,
        default=120,
        help="stop when nothing changes for this many seconds (default 120); a note or a "
        "heartbeat of a running task is a change",
    )
    found.add_argument(
        "--database",
        type=Path,
        default=None,
        help="the SQLite file to make (default: a new file in a temporary folder)",
    )
    found.add_argument("--json", action="store_true", help="print the report as JSON")
    return found


def main(argv: Sequence[str], *, skill_version: str) -> int:
    """Run the try-out script with the arguments `argv`. Returns the exit code."""
    try:
        options = parser().parse_args(list(argv))
        context = _context(cast("str", options.context))
        fires = [parse_fire(text) for text in cast("list[str]", options.fire)]
    except WrongUse as error:
        print(f"wrong use: {error}; see --help", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"wrong use: {error}", file=sys.stderr)
        return 2
    installed = version("stateous")
    if skill_version != installed:
        return _error(
            f"this skill is for Stateous {skill_version}, and this project has {installed}; "
            "run: stateous install skill DIR --force"
        )
    folder = cast("Path", options.folder)
    if _imports_phd(folder) and find_spec("phd") is None:
        return _error(
            'the routines import PHD, and it is not installed; run: pip install "stateous[phd]"'
        )
    try:
        report = asyncio.run(_run(options, context, fires))
    except (
        SupervisorError,
        ControlPlaneError,
        GraphFileError,
        DatabaseHeld,
        ForeignDatabase,
        SchemaNotReady,
        OSError,
    ) as error:
        return _error(str(error) or type(error).__name__)
    if options.json:
        print(json.dumps(report.to_json(), indent=2))
    else:
        print(report.to_text())
    return 0 if report.stop.reason == "completed" else 1


async def _run(options: argparse.Namespace, context: dict[str, Any], fires: list[Fire]) -> Any:
    return await try_workflow(
        cast("Path", options.graph),
        cast("Path", options.folder),
        context,
        fires=fires,
        timeout=cast("float", options.timeout),
        quiet=cast("float", options.quiet),
        database=cast("Path | None", options.database),
    )


def _context(text: str) -> dict[str, Any]:
    try:
        value: object = json.loads(text)
    except json.JSONDecodeError:
        raise WrongUse("--context is not JSON") from None
    if not isinstance(value, dict):
        raise WrongUse("--context is not a JSON object")
    return cast("dict[str, Any]", value)


def _imports_phd(folder: Path) -> bool:
    """Whether a Python file of `folder` imports `phd`. A file that does not parse is
    not counted: the supervisor names it when it loads the routines."""
    for path in sorted(folder.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name.split(".")[0] == "phd" for alias in node.names
            ):
                return True
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "phd":
                return True
    return False


def _error(sentence: str) -> int:
    print(f"error: {sentence}", file=sys.stderr)
    return 1
