"""Output to a closed pipe, as in `stateous token show | head -1`: the command ends
quietly, with no traceback."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
FLOW = ROOT / "examples" / "sdlc" / "flow.py"


def with_stdout_closed(*arguments: str) -> tuple[int, str]:
    """Run `python -m stateous ARGUMENTS`, with the reading end of its stdout closed
    before the command writes. Returns the exit code and stderr."""
    process = subprocess.Popen(
        [sys.executable, "-m", "stateous", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ROOT,
    )
    assert process.stdout is not None and process.stderr is not None
    process.stdout.close()  # the process needs more than a second to import: it writes later
    try:
        error = process.stderr.read().decode("utf-8", errors="replace")
        return process.wait(timeout=120), error
    finally:
        process.kill()
        process.stderr.close()


@pytest.mark.parametrize("command", ["help", "compile"])
def test_a_closed_pipe_ends_the_command_quietly(command: str, tmp_path: Path) -> None:
    if command == "help":
        arguments = ("token", "--help")
    else:
        arguments = ("compile", str(FLOW), "--out", str(tmp_path / "sdlc.json"))

    code, error = with_stdout_closed(*arguments)

    assert code in (0, 1)
    assert error == ""
