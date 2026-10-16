"""The example: its committed files are current, its two folders hold what the
design says (section 3), and its `testing` routine does what the docs say.

If a test of a committed file fails after a change to the compiler or the code
generator, write the files again. First run `uv run stateous compile
examples/sdlc/flow.py`. Then write examples/sdlc-supervisor/contract.py with
`generate_contract(load_graph(text))`, where `text` is the new graph file.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from stateous import Context, Occurrence
from stateous.compiler.codegen import generate_contract
from stateous.model.contract import implementation_of
from stateous.model.graph_file import load_graph
from stateous.model.wire import EventRecord, TaskGrant

ROOT = Path(__file__).parents[2]
FLOW = ROOT / "examples" / "sdlc"
FOLDER = ROOT / "examples" / "sdlc-supervisor"
GRAPH = FLOW / "build" / "sdlc.json"
STATEOUS = [sys.executable, "-m", "stateous"]
MODULES = ("contract", "testing", "coding")
PASSING = "def test_ok() -> None:\n    assert True\n"
FAILING = "def test_not_ok() -> None:\n    assert 1 == 2\n"


def test_the_committed_graph_file_is_current(tmp_path: Path) -> None:
    out = tmp_path / "sdlc.json"
    done = subprocess.run(
        [*STATEOUS, "compile", str(FLOW / "flow.py"), "--out", str(out)],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert out.read_text(encoding="utf-8") == GRAPH.read_text(encoding="utf-8")


def test_the_graph_is_the_workflow_of_the_design() -> None:
    graph = load_graph(GRAPH.read_text(encoding="utf-8"))

    assert (graph["stateous"], graph["workflow"], graph["context"]) == (1, "sdlc", "Ticket")
    assert graph["states"] == {
        "coding": {"timeout": "6h", "retries": 2, "emits": ["done", "stuck"]},
        "testing": {"timeout": "1h", "emits": ["passed", "failed"]},
    }
    assert graph["events"] == {
        "done": {},
        "stuck": {},
        "passed": {"payload": "Tests"},
        "failed": {"payload": "Tests"},
    }
    assert graph["transitions"] == [
        {"from": "__start__", "to": "coding"},
        {"from": "coding", "to": "testing", "on": "done"},
        {"from": "coding", "to": "__end__", "on": "stuck"},
        {"from": "testing", "to": "__end__", "on": "passed"},
        {"from": "testing", "to": "coding", "on": "failed", "when": "count('failed') < 3"},
        {"from": "testing", "to": "__end__", "on": "failed", "otherwise": True},
    ]
    assert set(graph["schemas"]) == {"Ticket", "Tests"}


@pytest.fixture
def on_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The supervisor's folder first on sys.path, as the supervisor has it; the
    example's modules imported fresh, and removed again after the test."""
    saved = {name: sys.modules.pop(name) for name in MODULES if name in sys.modules}
    monkeypatch.syspath_prepend(str(FOLDER))  # pyright: ignore[reportUnknownMemberType]
    yield
    for name in MODULES:
        sys.modules.pop(name, None)
    sys.modules.update(saved)


def context(state: str, repo: str) -> Context[Any]:
    grant = TaskGrant(
        task=1,
        token="TICKET-1",
        state=state,
        visit=1,
        attempt=1,
        context={"repo": repo},
        memory={},
        entered_by=EventRecord(name="done"),
    )

    def save(key: str, value: Any) -> None:
        raise AssertionError(f"the testing routine saves no memory ({key}={value!r})")

    def note(text: str) -> None:
        raise AssertionError(f"the testing routine writes no notes ({text!r})")

    return Context(grant, save_memory=save, note=note)


def checkout(folder: Path, *tests: str) -> Path:
    folder.mkdir()
    for number, test in enumerate(tests):
        (folder / f"test_{number}.py").write_text(test, encoding="utf-8", newline="\n")
    return folder


def test_the_committed_contract_is_current() -> None:
    graph = load_graph(GRAPH.read_text(encoding="utf-8"))

    assert (FOLDER / "contract.py").read_text(encoding="utf-8") == generate_contract(graph)


def test_the_supervisor_folder_holds_no_workflow() -> None:
    names = {path.name for path in FOLDER.iterdir()} - {"__pycache__"}

    assert {"contract.py", "testing.py", "stateous.yaml"} <= names
    assert not names & {"flow.py", "schemas.py", "build"}
    assert not list(FOLDER.rglob("*.json"))


def test_the_supervisor_settings_reach_the_local_control_plane() -> None:
    settings = yaml.safe_load((FOLDER / "stateous.yaml").read_text(encoding="utf-8"))

    assert settings == {"url": "http://127.0.0.1:8080", "tasks": 4}


def test_testing_is_the_routine_of_the_testing_state(on_path: None) -> None:
    testing = importlib.import_module("testing")

    state = implementation_of(testing.test)
    assert state is not None
    assert state.name == "testing"


def test_testing_reports_passed_when_every_test_passes(tmp_path: Path, on_path: None) -> None:
    testing = importlib.import_module("testing")
    repo = checkout(tmp_path / "repo", PASSING, PASSING)

    outcome: Occurrence[Any] = testing.test(context("testing", str(repo)))

    assert type(outcome).name == "passed"
    assert (outcome.payload.passed, outcome.payload.failed) == (2, 0)


def test_testing_reports_failed_when_a_test_fails(tmp_path: Path, on_path: None) -> None:
    testing = importlib.import_module("testing")
    repo = checkout(tmp_path / "repo", PASSING, FAILING)

    outcome: Occurrence[Any] = testing.test(context("testing", str(repo)))

    assert type(outcome).name == "failed"
    assert (outcome.payload.passed, outcome.payload.failed) == (1, 1)


def test_testing_fails_when_the_checkout_is_missing(tmp_path: Path, on_path: None) -> None:
    testing = importlib.import_module("testing")

    with pytest.raises(FileNotFoundError, match="no checkout"):
        testing.test(context("testing", str(tmp_path / "nowhere")))
