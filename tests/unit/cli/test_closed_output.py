"""Output to a closed pipe, as in `stateous token show | head -1`: the command ends
quietly, with no traceback."""

from __future__ import annotations

import errno
import io
import os
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


def run_main(*arguments: str) -> None:
    """Run `stateous ARGUMENTS` in this process, as the console script does."""
    from stateous.cli.main import main

    old = sys.argv
    sys.argv = ["stateous", *arguments]
    try:
        main()
    finally:
        sys.argv = old


def test_einval_from_a_command_is_not_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows, EINVAL also comes from, for example, `open('a?b.py')`. When it does
    not come from a write to stdout, the command does not hide it."""
    import stateous.compiler.load

    def broken(path: Path) -> object:
        raise OSError(errno.EINVAL, "Invalid argument", str(path))

    monkeypatch.setattr(stateous.compiler.load, "load_workflow", broken)

    with pytest.raises(OSError) as raised:
        run_main("compile", str(FLOW), "--out", str(tmp_path / "sdlc.json"))

    assert raised.value.errno == errno.EINVAL


class ClosedStdout(io.StringIO):
    """Stdout whose reader closed the pipe, as Windows reports it: EINVAL."""

    def __init__(self, fd: int) -> None:
        super().__init__()
        self.fd = fd

    def write(self, text: str) -> int:
        raise OSError(errno.EINVAL, "Invalid argument")

    def fileno(self) -> int:
        return self.fd


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reports a closed pipe as EINVAL")
def test_einval_from_a_write_to_stdout_is_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fd = os.open(tmp_path / "stdout", os.O_WRONLY | os.O_CREAT)
    try:
        monkeypatch.setattr(sys, "stdout", ClosedStdout(fd))

        with pytest.raises(SystemExit) as raised:
            run_main("compile", str(FLOW), "--out", str(tmp_path / "sdlc.json"))
    finally:
        os.close(fd)

    assert raised.value.code == 1
    assert capsys.readouterr().err == ""
