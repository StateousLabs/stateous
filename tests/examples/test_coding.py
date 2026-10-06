"""The `coding` routine of the example, run against the stand-in for PHD.

PHD is not installed in the tests, and the features that coding.py uses are not
released yet (docs/design.md, section 6 and N8). tests/examples/phd_stub has the
same names and shapes. These tests check what Stateous's side does: the memory,
the notes, the outcome, and a failed run.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import importlib
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from stateous import Context, Occurrence, OutcomeError
from stateous.model.wire import TaskGrant

ROOT = Path(__file__).parents[2]
FOLDER = ROOT / "examples" / "sdlc-worker"
STUB = Path(__file__).parent / "phd_stub"
MODULES = ("contract", "testing", "coding", "phd")
PHD_SPEC = ROOT.parent / "poly-harness-dispatcher" / "src" / "phd" / "spec" / "spec.py"
"""The real PHD `Spec`, when its source is next to this repository. The test reads it; it never
imports PHD."""


@dataclass
class Sent:
    """What the routine sent to the control plane: memory keys and notes."""

    memory: list[tuple[str, Any]] = field(default_factory=list[tuple[str, Any]])
    notes: list[str] = field(default_factory=list[str])

    def save(self, key: str, value: Any) -> None:
        self.memory.append((key, value))

    def note(self, text: str) -> None:
        self.notes.append(text)


@pytest.fixture
def on_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The supervisor's folder and the stand-in for PHD first on sys.path; the
    modules imported fresh, and removed again after the test."""
    saved = {name: sys.modules.pop(name) for name in MODULES if name in sys.modules}
    monkeypatch.syspath_prepend(str(STUB))  # pyright: ignore[reportUnknownMemberType]
    monkeypatch.syspath_prepend(str(FOLDER))  # pyright: ignore[reportUnknownMemberType]
    yield
    for name in MODULES:
        sys.modules.pop(name, None)
    sys.modules.update(saved)


def modules() -> tuple[ModuleType, ModuleType, ModuleType]:
    """The stand-in for PHD, the contract, and coding.py."""
    phd = importlib.import_module("phd")
    contract = importlib.import_module("contract")
    coding = importlib.import_module("coding")
    return phd, contract, coding


def context(sent: Sent, memory: dict[str, Any] | None = None) -> Context[Any]:
    grant = TaskGrant(
        task=7,
        token="TICKET-1",
        state="coding",
        visit=1,
        attempt=1,
        context={"repo": "acme/api"},
        memory=memory or {},
        entered_by=None,
    )
    return Context(grant, save_memory=sent.save, note=sent.note)


def test_coding_runs_the_agent_and_returns_its_outcome(on_path: None) -> None:
    phd, contract, coding = modules()
    sent = Sent()

    outcome: Occurrence[Any] = asyncio.run(coding.code(context(sent)))

    assert type(outcome).name == "done"
    assert sent.memory == [("dev", "session-1")]  # saved at once, so a retry can resume
    assert sent.notes == ["run.started", "wrote the code"]
    (call,) = phd.Dispatcher.calls
    assert call.session_id is None
    assert call.inputs.msg == "Implement acme/api"
    assert call.spec.cwd == "acme/api"
    assert call.spec.stack == phd.Stack("stacks/dev")
    assert call.spec.credentials == phd.Credentials("claude")
    assert call.spec.idle_timeout_s == 900
    assert call.spec.output_schema == contract.states.coding.outcome_schema()


def test_coding_resumes_the_session_in_its_memory(on_path: None) -> None:
    phd, _, coding = modules()
    sent = Sent()

    asyncio.run(coding.code(context(sent, memory={"dev": "session-0"})))

    (call,) = phd.Dispatcher.calls
    assert call.session_id == "session-0"
    assert sent.memory == []  # the key is there already


def test_a_failed_run_fails_the_attempt(on_path: None) -> None:
    phd, _, coding = modules()
    error = phd.RunError(kind="schema", message="the answer does not fit the schema")
    phd.Dispatcher.script = phd.Script(result=phd.RunResult(status="failed", error=error))

    with pytest.raises(RuntimeError, match="schema"):
        asyncio.run(coding.code(context(Sent())))


def test_an_answer_with_an_event_the_state_cannot_emit_is_refused(on_path: None) -> None:
    phd, _, coding = modules()
    answer = {"event": "passed", "payload": {"passed": 1, "failed": 0}}
    phd.Dispatcher.script = phd.Script(
        result=phd.RunResult(status="succeeded", structured_output=answer)
    )

    with pytest.raises(OutcomeError):
        asyncio.run(coding.code(context(Sent())))


def spec_fields(source: str) -> tuple[list[str], set[str]]:
    """The fields of the class `Spec` in PHD's source: all, and the required ones."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == "Spec":
            fields = [
                item
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            ]
            names = [field.target.id for field in fields if isinstance(field.target, ast.Name)]
            required = {
                field.target.id
                for field in fields
                if isinstance(field.target, ast.Name) and field.value is None
            }
            return names, required
    raise AssertionError("PHD's source has no class Spec")


@pytest.mark.skipif(not PHD_SPEC.is_file(), reason="the PHD source is not next to this repository")
def test_the_stand_in_spec_needs_the_fields_that_phd_needs(on_path: None) -> None:
    phd, _, _ = modules()
    names, required = spec_fields(PHD_SPEC.read_text(encoding="utf-8"))
    stand_in = dataclasses.fields(phd.Spec)
    no_default = dataclasses.MISSING

    stand_in_required = {
        field.name
        for field in stand_in
        if field.default is no_default and field.default_factory is no_default
    }
    assert stand_in_required == required
    assert {field.name for field in stand_in} <= set(names)
