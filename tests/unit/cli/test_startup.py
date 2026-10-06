"""The light commands start fast: `stateous --help`, `stateous token ...` and
`stateous compile` do not import the server, the database or the supervisor.

Each case runs in a new Python process, because the test process imported these
modules already.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
FLOW = ROOT / "examples" / "sdlc" / "flow.py"
HEAVY = ("sqlalchemy", "uvicorn", "starlette", "alembic", "stateous.control", "stateous.supervisor")
PROBE = """
import json, sys
from stateous.cli.main import app
try:
    app(sys.argv[1:], prog_name="stateous")
except SystemExit:
    pass
heavy = sorted({name.split(".")[0] if not name.startswith("stateous.") else
                ".".join(name.split(".")[:2]) for name in sys.modules})
print(json.dumps(heavy), file=sys.stderr)
"""


def imported(*arguments: str) -> set[str]:
    """The top-level modules (and `stateous.X` packages) that a command imports."""
    done = subprocess.run(
        [sys.executable, "-c", PROBE, *arguments],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=120,
    )
    last = done.stderr.strip().splitlines()[-1]
    return set(json.loads(last))


@pytest.mark.parametrize(
    "arguments",
    [
        ("--help",),
        ("token", "--help"),
        ("token", "show", "--help"),
        ("compile", "--help"),
    ],
)
def test_a_light_command_imports_no_server_and_no_database(arguments: tuple[str, ...]) -> None:
    found = imported(*arguments)

    assert "stateous.cli" in found  # the probe ran the command
    assert not found & set(HEAVY), sorted(found & set(HEAVY))


def test_compile_imports_no_server_and_no_database(tmp_path: Path) -> None:
    found = imported("compile", str(FLOW), "--out", str(tmp_path / "sdlc.json"))

    assert (tmp_path / "sdlc.json").is_file()
    assert not found & set(HEAVY), sorted(found & set(HEAVY))
