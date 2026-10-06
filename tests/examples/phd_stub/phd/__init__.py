"""A stand-in for the PHD features that `examples/sdlc-worker/coding.py` uses.

PHD is not a dependency of Stateous. The PHD author accepted these features on
2026-10-06, but they are not released yet: `Dispatcher.run()`, `handle.session_id`,
`event.summary` and `Spec.idle_timeout_s` (docs/design.md, section 6 and N8). The
names and shapes here follow that agreement. Pyright checks coding.py against this
file, and tests/examples/test_coding.py runs coding.py with it. `Spec` needs the
same fields as the real PHD `Spec`; a test compares them when the PHD source is there.

A test sets `Dispatcher.script` to choose what the next run does. It reads
`Dispatcher.calls` to see how the routine called PHD.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, ClassVar


@dataclass(frozen=True)
class Stack:
    """The agent: a stack folder. The stand-in does not read the folder."""

    root: str

    @classmethod
    def load(cls, root: str) -> Stack:
        return cls(root)


@dataclass(frozen=True)
class Credentials:
    """How a run authenticates. The stand-in only keeps the harness."""

    harness: str

    @classmethod
    def from_host(cls, harness: str) -> Credentials:
        return cls(harness)


@dataclass(frozen=True)
class Spec:
    harness: str
    model: str
    stack: Stack
    cwd: str
    credentials: Credentials
    output_schema: dict[str, Any] | None = None
    idle_timeout_s: float | None = None


@dataclass(frozen=True)
class Inputs:
    msg: str


@dataclass(frozen=True)
class Event:
    type: str
    summary: str


@dataclass(frozen=True)
class RunError:
    kind: str
    message: str


@dataclass(frozen=True)
class RunResult:
    status: str
    structured_output: Any = None
    error: RunError | None = None


def _succeeded() -> RunResult:
    return RunResult(status="succeeded", structured_output={"event": "done"})


@dataclass(frozen=True)
class Script:
    """What the next run does: its session id, its events, its result."""

    session_id: str = "session-1"
    summaries: tuple[str, ...] = ("run.started", "wrote the code")
    result: RunResult = field(default_factory=_succeeded)


@dataclass(frozen=True)
class Call:
    """One call of `Dispatcher(spec).run(inputs, session_id=...)`."""

    spec: Spec
    inputs: Inputs
    session_id: str | None


class RunHandle:
    def __init__(self, script: Script) -> None:
        self._script = script
        self.session_id: str | None = None

    async def events(self) -> AsyncIterator[Event]:
        for summary in self._script.summaries:
            self.session_id = self._script.session_id  # known after run.started
            yield Event(type="message", summary=summary)

    async def wait(self) -> RunResult:
        return self._script.result


class Run:
    def __init__(self, handle: RunHandle) -> None:
        self._handle = handle

    async def __aenter__(self) -> RunHandle:
        return self._handle

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class Dispatcher:
    script: ClassVar[Script] = Script()
    calls: ClassVar[list[Call]] = []

    def __init__(self, spec: Spec) -> None:
        self.spec = spec

    def run(self, inputs: Inputs, *, session_id: str | None = None) -> Run:
        Dispatcher.calls.append(Call(self.spec, inputs, session_id))
        return Run(RunHandle(Dispatcher.script))
