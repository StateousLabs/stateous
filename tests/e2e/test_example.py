"""The example, end to end, as real processes.

`stateous compile`, `serve`, `init`, `supervise` and `token ...` each run as a
process of their own, as in docs/guide.md, "Try it on one computer". The control
plane listens on a free port of 127.0.0.1 and keeps a SQLite file in the test's
folder. The supervisor runs testing.py from the example. Nobody runs coding.py,
because it needs PHD (docs/design.md, section 6). The test reports `coding` from
outside, as CI or a person does: it fires `done`.

The stop signal is real: SIGTERM on POSIX. On Windows, it is CTRL_BREAK_EVENT to a
process started in a process group of its own.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx2
import pytest
import yaml

from stateous import Client

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "examples"
STATEOUS = [sys.executable, "-m", "stateous"]

PASSING = "def test_ok() -> None:\n    assert True\n"
FAILING = "def test_not_ok() -> None:\n    assert 1 == 2\n"
GATED = (
    "import pathlib\n"
    "import time\n"
    "\n"
    "\n"
    "def test_waits_for_the_gate() -> None:\n"
    "    gate = pathlib.Path(__file__).with_name('gate')\n"
    "    for _ in range(600):\n"
    "        if gate.exists():\n"
    "            return\n"
    "        time.sleep(0.1)\n"
    "    raise AssertionError('the gate did not open')\n"
)
"""A test that waits until the file `gate` exists (60 seconds at most)."""


@dataclass
class Processes:
    """The commands of the test: their environment, and the processes that still run."""

    url: str
    started: list[subprocess.Popen[bytes]] = field(default_factory=list[subprocess.Popen[bytes]])

    def env(self) -> dict[str, str]:
        return {
            **os.environ,
            "STATEOUS_URL": self.url,
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
        }

    def run(self, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*STATEOUS, *args],
            cwd=cwd,
            env=self.env(),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
        )

    def ok(self, *args: str, cwd: Path) -> str:
        done = self.run(*args, cwd=cwd)
        assert done.returncode == 0, f"stateous {' '.join(args)}\n{done.stdout}\n{done.stderr}"
        return done.stdout

    def start(self, *args: str, cwd: Path, log: Path) -> subprocess.Popen[bytes]:
        """A long-running command, in a process group of its own: a signal reaches it
        alone, and its whole tree can be stopped."""
        with log.open("wb") as sink:
            if sys.platform == "win32":
                process = subprocess.Popen(
                    [*STATEOUS, *args],
                    cwd=cwd,
                    env=self.env(),
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                process = subprocess.Popen(
                    [*STATEOUS, *args],
                    cwd=cwd,
                    env=self.env(),
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        self.started.append(process)
        return process


def interrupt(process: subprocess.Popen[bytes]) -> None:
    """What Ctrl-C, or a service manager, sends."""
    if sys.platform == "win32":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        process.send_signal(signal.SIGTERM)


def stop_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill a process and all that it started, if it still runs."""
    if process.poll() is None:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, timeout=60
            )
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=30)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def answers(url: str) -> bool:
    """Whether the control plane answers `GET /health`."""
    try:
        return httpx2.get(f"{url}/health", timeout=5).status_code == 200
    except httpx2.HTTPError:
        return False


def wait_for(condition: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.25)
    return condition()


def log(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def checkout(folder: Path, test: str) -> Path:
    folder.mkdir(parents=True)
    (folder / "test_it.py").write_text(test, encoding="utf-8", newline="\n")
    return folder


def ticket(repo: Path) -> str:
    return json.dumps({"repo": str(repo)})


def kinds(client: Client, token: str) -> list[str]:
    return [str(entry["kind"]) for entry in client.history(token)]


def accepted(client: Client, token: str) -> list[str]:
    """The events the token accepted, without Stateous's internal `__started__`."""
    names = [
        str(entry["data"]["event"])
        for entry in client.history(token)
        if entry["kind"] == "event_accepted"
    ]
    return [name for name in names if not name.startswith("__")]


def entered(client: Client, token: str) -> list[str]:
    """The states the token entered, without START and END."""
    names = [
        str(entry["data"]["state"]) for entry in client.history(token) if entry["kind"] == "entered"
    ]
    return [name for name in names if not name.startswith("__")]


def has_status(client: Client, token: str, status: str) -> Callable[[], bool]:
    def check() -> bool:
        return client.get(token)["status"] == status

    return check


def reaches(client: Client, token: str, state: str, visit: int) -> Callable[[], bool]:
    def check() -> bool:
        now = client.get(token)
        return (now["state"], now["visit"], now["status"]) == (state, visit, "active")

    return check


def one_sentence(stderr: str) -> bool:
    return len(stderr.strip().splitlines()) == 1 and "Traceback" not in stderr


@pytest.fixture
def processes(tmp_path: Path) -> Iterator[Processes]:
    made = Processes(url=f"http://127.0.0.1:{free_port()}")
    try:
        yield made
    finally:
        for repo in (tmp_path / "repos").glob("*"):
            (repo / "gate").write_text("open", encoding="utf-8")  # a waiting test ends now
        for process in made.started:
            stop_tree(process)


def test_the_example_runs_as_real_processes(tmp_path: Path, processes: Processes) -> None:
    url = processes.url
    listen = url.removeprefix("http://")

    # ── The control plane's computer: compile, then serve ──────────────────
    plane = tmp_path / "plane"
    shutil.copytree(
        EXAMPLES / "sdlc", plane / "sdlc", ignore=shutil.ignore_patterns("build", "__pycache__")
    )
    processes.ok("compile", "sdlc/flow.py", cwd=plane)
    written = (plane / "sdlc" / "build" / "sdlc.json").read_text(encoding="utf-8")
    assert written == (EXAMPLES / "sdlc" / "build" / "sdlc.json").read_text(encoding="utf-8")

    database = f"sqlite:///{(tmp_path / 'plane.db').as_posix()}"
    serve_log = tmp_path / "serve.log"
    processes.start(
        "serve", "sdlc/build/sdlc.json", "--db", database, "--listen", listen,
        cwd=plane, log=serve_log,
    )  # fmt: skip
    assert wait_for(lambda: answers(url), 60), log(serve_log)

    # one control plane serves one database: a second one refuses to start
    second = processes.run(
        "serve", "sdlc/build/sdlc.json", "--db", database,
        "--listen", f"127.0.0.1:{free_port()}", cwd=plane,
    )  # fmt: skip
    assert second.returncode == 1, second.stdout + second.stderr
    assert second.stderr.strip() and "Traceback" not in second.stderr, second.stderr

    # ── The supervisor's computer: init, then supervise ────────────────────
    computer = tmp_path / "computer"
    folder = computer / "sdlc-supervisor"
    folder.mkdir(parents=True)
    shutil.copy(EXAMPLES / "sdlc-supervisor" / "testing.py", folder / "testing.py")
    processes.ok("init", url, "sdlc-supervisor", cwd=computer)

    example = EXAMPLES / "sdlc-supervisor"
    contract = (folder / "contract.py").read_text(encoding="utf-8")
    assert contract == (example / "contract.py").read_text(encoding="utf-8")
    kept = (folder / "testing.py").read_text(encoding="utf-8")
    assert kept == (example / "testing.py").read_text(encoding="utf-8")  # implemented: kept
    assert (folder / "coding.py").is_file()  # a stub for the state with no routine
    settings = yaml.safe_load((folder / "stateous.yaml").read_text(encoding="utf-8"))
    assert settings["url"] == url
    pyproject = tomllib.loads((folder / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["name"] == "sdlc-supervisor"
    (folder / "coding.py").unlink()  # nobody here runs `coding`: it is reported from outside

    # a contract that does not agree with the control plane: supervise refuses
    stale = computer / "stale"
    shutil.copytree(folder, stale, ignore=shutil.ignore_patterns("__pycache__"))
    found = re.search(r'^__checksum__ = "(sha256:[0-9a-f]+)"$', contract, re.MULTILINE)
    assert found is not None, contract
    (stale / "contract.py").write_text(
        contract.replace(found.group(1), "sha256:" + "0" * 64), encoding="utf-8", newline="\n"
    )
    refused = processes.run("supervise", "stale", cwd=computer)
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "stateous init" in refused.stderr, refused.stderr

    supervise_log = tmp_path / "supervise.log"
    supervising = processes.start("supervise", "sdlc-supervisor", cwd=computer, log=supervise_log)

    # ── The tokens ─────────────────────────────────────────────────────────
    passing = checkout(tmp_path / "repos" / "passing", PASSING)
    failing = checkout(tmp_path / "repos" / "failing", FAILING)
    gated = checkout(tmp_path / "repos" / "gated", GATED)

    with Client(url) as client:
        # TICKET-1: coding is reported from outside (a webhook, twice); testing passes
        processes.ok("token", "start", "TICKET-1", ticket(passing), cwd=tmp_path)
        assert client.get("TICKET-1")["state"] == "coding"
        processes.ok("token", "fire", "TICKET-1", "done", "--key", "gh-delivery-1", cwd=tmp_path)
        again = httpx2.post(
            f"{url}/tokens/TICKET-1/events/done",
            headers={"Idempotency-Key": "gh-delivery-1"},
            timeout=30,
        )
        assert again.status_code == 200, again.text
        assert again.json()["duplicate"] is True
        assert wait_for(has_status(client, "TICKET-1", "completed"), 90), log(supervise_log)
        assert accepted(client, "TICKET-1") == ["done", "passed"]
        assert "task_claimed" in kinds(client, "TICKET-1")
        shown = processes.ok("token", "show", "TICKET-1", cwd=tmp_path)
        assert "TICKET-1" in shown and "completed" in shown, shown

        # TICKET-2: the tests fail; the guard sends it back to coding two times
        processes.ok("token", "start", "TICKET-2", ticket(failing), cwd=tmp_path)
        for visit in (1, 2, 3):
            assert wait_for(reaches(client, "TICKET-2", "coding", visit), 90), log(supervise_log)
            processes.ok("token", "fire", "TICKET-2", "done", cwd=tmp_path)
        assert wait_for(has_status(client, "TICKET-2", "completed"), 90), log(supervise_log)
        assert accepted(client, "TICKET-2") == ["done", "failed"] * 3
        assert entered(client, "TICKET-2") == ["coding", "testing"] * 3

        # TICKET-3: a paused token holds its events; resume applies them
        processes.ok("token", "start", "TICKET-3", ticket(passing), cwd=tmp_path)
        processes.ok("token", "pause", "TICKET-3", cwd=tmp_path)
        processes.ok("token", "fire", "TICKET-3", "done", cwd=tmp_path)
        held = client.get("TICKET-3")
        assert (held["state"], held["status"]) == ("coding", "paused")
        assert "event_deferred" in kinds(client, "TICKET-3")
        processes.ok("token", "resume", "TICKET-3", cwd=tmp_path)
        assert wait_for(has_status(client, "TICKET-3", "completed"), 90), log(supervise_log)
        assert {"paused", "resumed", "event_replayed"} <= set(kinds(client, "TICKET-3"))

        # TICKET-4: a cancelled token refuses events, in one plain sentence
        processes.ok("token", "start", "TICKET-4", ticket(passing), cwd=tmp_path)
        processes.ok("token", "cancel", "TICKET-4", cwd=tmp_path)
        assert client.get("TICKET-4")["status"] == "cancelled"
        late = processes.run("token", "fire", "TICKET-4", "done", cwd=tmp_path)
        assert late.returncode == 1, late.stdout
        assert one_sentence(late.stderr), late.stderr

        # the list, and the graph
        listed = processes.ok("token", "show", cwd=tmp_path)
        for token in ("TICKET-1", "TICKET-2", "TICKET-3", "TICKET-4"):
            assert token in listed, listed
        drawn = processes.ok("token", "show", "--graph", cwd=tmp_path)
        assert "coding" in drawn and "testing" in drawn, drawn

        # TICKET-5: a graceful stop waits for the running routine, which then reports
        processes.ok("token", "start", "TICKET-5", ticket(gated), cwd=tmp_path)
        processes.ok("token", "fire", "TICKET-5", "done", cwd=tmp_path)
        claimed = wait_for(lambda: "task_claimed" in kinds(client, "TICKET-5"), 60)
        assert claimed, log(supervise_log)
        interrupt(supervising)
        assert not wait_for(lambda: supervising.poll() is not None, 3), log(supervise_log)
        (gated / "gate").write_text("open", encoding="utf-8")
        assert supervising.wait(timeout=60) == 0, log(supervise_log)
        assert client.get("TICKET-5")["status"] == "completed"
