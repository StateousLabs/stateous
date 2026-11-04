"""The command line of the skill's try-out script, `scripts/try_workflow.py`.

Exit code 0 when the top token completed, 1 when the try-out stopped for another
reason or could not run (one sentence on stderr), and 2 for wrong use.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from importlib.metadata import version
from importlib.util import find_spec
from pathlib import Path
from typing import Any, NoReturn, cast

from stateous.control.errors import ControlPlaneError
from stateous.control.store.errors import DatabaseHeld, ForeignDatabase, SchemaNotReady
from stateous.model.graph_file import GraphFileError
from stateous.supervisor.supervisor import SupervisorError
from stateous.tryout.run import GRACE, Fire, parse_fire, try_workflow

DESCRIPTION = """\
Try out a Stateous workflow in one process, with the real routines. It makes a new
SQLite database, starts a control plane, the supervisor of SUPERVISOR_FOLDER and one
token, and stops by itself. Then it prints a report: why it stopped, each task, and
the history of each token. Exit code 0: the token completed. 1: it stopped for
another reason. 2: wrong use."""

FIRE_HELP = """\
An answer for a state that waits: when a token waits at STATE, fire EVENT on it, with
the payload if given. Give it once for each answer. Ask the developer for the answer."""


WATCHDOG_MARGIN = 2 * GRACE + 10
"""Seconds after the time limit before the watchdog ends the process."""


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
        "--context",
        required=True,
        metavar="JSON",
        help="the context of the token, a JSON object; or @FILE for a JSON file (use a file "
        "in PowerShell, which changes the quotes of a JSON argument)",
    )
    found.add_argument(
        "--fire",
        action="append",
        default=[],
        metavar="STATE=EVENT[:PAYLOAD_JSON|:@FILE]",
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
            f"run with this Python: {sys.executable} -m stateous install skill DIR --force"
        )
    folder = cast("Path", options.folder)
    if _imports_phd(folder) and find_spec("phd") is None:
        return _error(
            'the routines import PHD, and it is not installed; run: pip install "stateous[phd]"'
        )
    database = cast("Path | None", options.database)
    if database is None:
        database = Path(tempfile.mkdtemp(prefix="stateous-try-")) / "try.db"
    limit = cast("float", options.timeout) + WATCHDOG_MARGIN
    watchdog = start_watchdog(limit, database)
    try:
        report = asyncio.run(_run(options, context, fires, database))
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
    except ValueError as error:
        print(f"wrong use: {error}", file=sys.stderr)
        return 2
    finally:
        watchdog.cancel()
    if options.json:
        print(json.dumps(report.to_json(), indent=2))
    else:
        print(report.to_text())
    return 0 if report.stop.reason == "completed" else 1


async def _run(
    options: argparse.Namespace, context: dict[str, Any], fires: list[Fire], database: Path
) -> Any:
    return await try_workflow(
        cast("Path", options.graph),
        cast("Path", options.folder),
        context,
        fires=fires,
        timeout=cast("float", options.timeout),
        quiet=cast("float", options.quiet),
        database=database,
    )


def start_watchdog(
    seconds: float, database: Path, *, exit: Callable[[int], object] = os._exit
) -> threading.Timer:
    """A daemon timer that ends the process after `seconds`, with one sentence. The time
    limit of the try-out is judged in the event loop: a routine that blocks the loop, or
    that does not end when it is cancelled, would hang the try-out without it."""

    def fire() -> None:
        print(
            f"error: the try-out did not stop {seconds:g} s after its start: a routine blocks "
            f"the event loop, or does not end when it is cancelled; the database is {database}",
            file=sys.stderr,
            flush=True,
        )
        exit(1)

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()
    return timer


def _context(text: str) -> dict[str, Any]:
    if text.startswith("@"):
        try:
            text = Path(text[1:]).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise WrongUse(f"cannot read the context file {text[1:]}") from None
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
