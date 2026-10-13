"""The `coding` routine of the example, run with the real PHD and a fake `Dispatcher`.

tests/examples/fake_phd.py fakes only `phd.Dispatcher` and the run handle, so no agent
starts. All else is the real `phd`. These tests check what Stateous's side does: the memory,
the notes, the outcome, and a failed run. Other tests check that the fake keeps the shape
of the real PHD.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import phd
import pytest

from stateous import Context, Occurrence, OutcomeError
from stateous.model.wire import TaskGrant

from .fake_phd import FakeDispatcher, FakeRunHandle, Script, result

ROOT = Path(__file__).parents[2]
FOLDER = ROOT / "examples" / "sdlc-supervisor"
MODULES = ("contract", "testing", "coding")
ERROR_KINDS_USED = ("session", "idle", "schema")
"""The error kinds that these tests give to coding.py."""


@dataclass
class Sent:
    """What the routine sent to the control plane: memory keys and notes."""

    memory: list[tuple[str, Any]] = field(default_factory=list[tuple[str, Any]])
    notes: list[str] = field(default_factory=list[str])

    def save(self, key: str, value: Any) -> None:
        self.memory.append((key, value))

    def note(self, text: str) -> None:
        self.notes.append(text)


def fake_credentials(harness: str) -> phd.Credentials:
    """In place of `Credentials.from_host`, which needs a login on this computer."""
    assert harness == "claude"
    return phd.Credentials.claude_oauth_token("test-token")


@pytest.fixture
def on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """The supervisor's folder first on sys.path; the example's modules imported fresh,
    and removed again after the test. The test runs in `tmp_path`, which has the stack
    folder `stacks/dev` and the repository `acme/api`. The fake takes the place of
    `phd.Dispatcher`, and `Credentials.from_host` gives a token."""
    saved = {name: sys.modules.pop(name) for name in MODULES if name in sys.modules}
    monkeypatch.syspath_prepend(str(FOLDER))  # pyright: ignore[reportUnknownMemberType]
    stack = tmp_path / "stacks" / "dev"
    (stack / "agents").mkdir(parents=True)
    (stack / "stack.json").write_text(json.dumps({"name": "dev", "version": "0.1.0"}), "utf-8")
    (stack / "agents" / "dev.md").write_text("You write code.\n", encoding="utf-8")
    (tmp_path / "acme" / "api").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phd, "Dispatcher", FakeDispatcher)
    monkeypatch.setattr(phd.Credentials, "from_host", fake_credentials)
    monkeypatch.setattr(FakeDispatcher, "script", Script())
    monkeypatch.setattr(FakeDispatcher, "calls", [])
    yield tmp_path
    for name in MODULES:
        sys.modules.pop(name, None)
    sys.modules.update(saved)


def modules() -> tuple[ModuleType, ModuleType]:
    """The contract and coding.py."""
    contract = importlib.import_module("contract")
    coding = importlib.import_module("coding")
    return contract, coding


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


def failed(status: phd.RunStatus, kind: str, message: str) -> Script:
    """A run that fails with a real `phd.RunError`."""
    error = phd.RunError(kind=kind, message=message)
    return Script(summaries=(), result=result(status, error=error, session_id=None))


def test_coding_runs_the_agent_and_returns_its_outcome(on_path: Path) -> None:
    contract, coding = modules()
    sent = Sent()

    outcome: Occurrence[Any] = asyncio.run(coding.code(context(sent)))

    assert type(outcome).name == "done"
    assert sent.memory == [("dev", "session-1")]  # saved at once, so a retry can resume
    assert sent.notes == ["wrote the code"]  # the empty summary of run.started gives no note
    (call,) = FakeDispatcher.calls
    assert call.session_id is None
    assert call.inputs.msg == "Implement acme/api"
    assert call.spec.cwd == (on_path / "acme" / "api").resolve()
    assert call.spec.stack.root == (on_path / "stacks" / "dev").resolve()
    assert call.spec.credentials.harness == "claude"
    assert call.spec.idle_timeout_s == 900
    schema: object = vars(call.spec)["output_schema"]  # PHD types it as a bare `dict`
    assert schema == contract.states.coding.outcome_schema()


def test_coding_resumes_the_session_in_its_memory(on_path: Path) -> None:
    _, coding = modules()
    sent = Sent()

    asyncio.run(coding.code(context(sent, memory={"dev": "session-0"})))

    (call,) = FakeDispatcher.calls
    assert call.session_id == "session-0"
    assert sent.memory == []  # the key is there already


def test_a_failed_run_fails_the_attempt(on_path: Path) -> None:
    _, coding = modules()
    FakeDispatcher.script = failed(
        phd.RunStatus.FAILED, "schema", "the answer does not fit the schema"
    )

    with pytest.raises(RuntimeError, match="schema"):
        asyncio.run(coding.code(context(Sent())))


def test_a_session_that_cannot_be_resumed_is_dropped_so_the_retry_starts_fresh(
    on_path: Path,
) -> None:
    _, coding = modules()
    FakeDispatcher.script = failed(
        phd.RunStatus.FAILED, "session", "no run with session 'session-0'"
    )
    sent = Sent()

    with pytest.raises(RuntimeError, match="session"):
        asyncio.run(coding.code(context(sent, memory={"dev": "session-0"})))

    assert sent.memory == [("dev", None)]  # deleted: the next attempt has no session id


def test_another_failure_keeps_the_session(on_path: Path) -> None:
    _, coding = modules()
    FakeDispatcher.script = failed(phd.RunStatus.TIMED_OUT, "idle", "no event for 900 s")
    sent = Sent()

    with pytest.raises(RuntimeError, match="idle"):
        asyncio.run(coding.code(context(sent, memory={"dev": "session-0"})))

    assert sent.memory == []  # the retry resumes the same session


def test_an_answer_with_an_event_the_state_cannot_emit_is_refused(on_path: Path) -> None:
    _, coding = modules()
    answer = {"event": "passed", "payload": {"passed": 1, "failed": 0}}
    FakeDispatcher.script = Script(result=result(phd.RunStatus.SUCCEEDED, structured_output=answer))

    with pytest.raises(OutcomeError):
        asyncio.run(coding.code(context(Sent())))


# ── The fake keeps the shape of the real PHD ─────────────────────────────


def shape(function: Any) -> list[tuple[str, Any, Any]]:
    """The parameters of a function: name, kind and default. Not the annotations: the
    real PHD and the fake write them in different modules."""
    return [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in inspect.signature(function).parameters.values()
    ]


def test_the_fake_run_has_the_parameters_of_the_real_run() -> None:
    assert shape(FakeDispatcher.run) == shape(phd.Dispatcher.run)


def test_the_real_run_handle_has_what_coding_uses_from_the_fake() -> None:
    # A real handle, made without a run: its __init__ only keeps its arguments.
    dispatcher = cast(Any, SimpleNamespace(adapter=None))
    home = cast(Any, SimpleNamespace(run_id="20261013-120000-abcdef"))
    real = phd.RunHandle(dispatcher, home, None, "")
    fake = FakeRunHandle(Script())

    for name in ("events", "wait", "session_id"):
        assert hasattr(real, name), name
        assert hasattr(fake, name), name
    assert inspect.isasyncgenfunction(phd.RunHandle.events)
    assert inspect.iscoroutinefunction(phd.RunHandle.wait)
    assert real.session_id is None  # as the fake, before the harness reports its session


def test_the_error_kinds_of_the_tests_are_real_phd_error_kinds() -> None:
    assert isinstance(phd.ERROR_KINDS, tuple)
    for kind in ERROR_KINDS_USED:
        assert kind in phd.ERROR_KINDS, kind
