"""A fake PHD `Dispatcher` for the tests of `examples/sdlc-supervisor/coding.py`.

It fakes only `Dispatcher` (its `run()` block) and the run handle. It launches no agent. It
plays a `Script`: a session id, some event summaries and a result. It records each call.
All other objects (`Spec`, `Inputs`, `Event`, `RunResult`, `RunError`, ...) are the real
ones from `phd`. tests/examples/test_coding.py checks that the fake has the same shape
as the real PHD.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import ClassVar

import phd


def result(
    status: phd.RunStatus,
    *,
    structured_output: object = None,
    error: phd.RunError | None = None,
    session_id: str | None = "session-1",
) -> phd.RunResult:
    """A real `phd.RunResult`, with plain values in the fields that the routine does not read."""
    return phd.RunResult(
        run_id="20261013-120000-abcdef",
        harness="claude",
        model="sonnet",
        stack="dev@0.1.0",
        stack_hash="",
        status=status,
        exit_code=0 if error is None else 1,
        error=error,
        result_text=None,
        structured_output=structured_output,
        cost_usd=None,
        cost_source="unknown",
        usage=phd.Usage(),
        num_turns=1,
        duration_ms=1,
        session_id=session_id,
        resumable=session_id is not None,
        resumed_from=None,
        run_dir="",
        home_dir="",
        cwd="",
        project_config="ignore",
        autonomy="restricted",
        unsafe=False,
        credential_kind="claude_oauth_token",
        started_at="2026-10-13T12:00:00+00:00",
        finished_at="2026-10-13T12:00:01+00:00",
    )


def _succeeded() -> phd.RunResult:
    return result(phd.RunStatus.SUCCEEDED, structured_output={"event": "done"})


@dataclass(frozen=True)
class Script:
    """What the next run does: its session id, the summaries of its events, its result."""

    session_id: str = "session-1"
    summaries: tuple[str, ...] = ("", "wrote the code")
    result: phd.RunResult = field(default_factory=_succeeded)


@dataclass(frozen=True)
class Call:
    """One call of `Dispatcher(spec).run(inputs, session_id=...)`."""

    spec: phd.Spec
    inputs: phd.Inputs
    session_id: str | None


class FakeRunHandle:
    """Has the attributes of `phd.RunHandle` that coding.py uses."""

    def __init__(self, script: Script) -> None:
        self._script = script
        self.session_id: str | None = None

    async def events(self) -> AsyncIterator[phd.Event]:
        for number, summary in enumerate(self._script.summaries):
            kind = "run.started" if number == 0 else "message"
            yield phd.Event(
                run_id="20261013-120000-abcdef",
                harness="claude",
                type=kind,
                data={},
                ts=0.0,
                summary=summary,
            )
            self.session_id = self._script.session_id  # the harness reported it

    async def wait(self) -> phd.RunResult:
        return self._script.result


class FakeDispatcher:
    """Put in place of `phd.Dispatcher`. A test sets `script` and reads `calls`."""

    script: ClassVar[Script] = Script()
    calls: ClassVar[list[Call]] = []

    def __init__(self, spec: phd.Spec) -> None:
        self.spec = spec

    @contextlib.asynccontextmanager
    async def run(
        self, inputs: phd.Inputs, *, session_id: str | None = None
    ) -> AsyncGenerator[FakeRunHandle]:
        FakeDispatcher.calls.append(Call(self.spec, inputs, session_id))
        yield FakeRunHandle(FakeDispatcher.script)
