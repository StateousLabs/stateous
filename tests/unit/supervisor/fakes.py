"""A fake control plane for the supervisor's unit tests, contract types, and helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection
from dataclasses import replace
from types import ModuleType
from typing import Any

from pydantic import BaseModel

from stateous.model.contract import StateRef
from stateous.model.events import Occurrence
from stateous.model.wire import Outcome, TaskGrant
from stateous.supervisor.registry import Registry, Routine
from stateous.supervisor.remote import Refused
from stateous.supervisor.supervisor import Supervisor


class Verdict(BaseModel):
    score: int


class scored(Occurrence[Verdict]):  # noqa: N801
    name = "scored"


class done(Occurrence[None]):  # noqa: N801
    name = "done"


TRIAGE = StateRef("triage", emits=(scored,))
CODING = StateRef("coding", emits=(done,))


class FakeApi:
    """Answers as the control plane would, and records every call."""

    url = "http://fake.test"

    def __init__(self, *grants: TaskGrant, checksum: str = "sha256:x") -> None:
        self.grants = list(grants)
        self.calls: list[tuple[Any, ...]] = []
        self.reports: dict[int, Outcome] = {}
        """Each task's last report, without its attempt (see `attempts`)."""
        self.attempts: list[tuple[str, int, int | None]] = []
        """The attempt that each report, note and memory write named, in order."""
        self.lost: set[int] = set()
        self.refuse_reports = False
        self.refuse_notes = False
        self.note_delay = 0.0
        self.checksum = checksum
        self.refusals: list[Refused] = []
        """The next reports refuse with these, in order."""
        self.claim_gate: asyncio.Event | None = None
        """When set: each claim waits for this event before it answers."""
        self.claim_waits = 0
        """The claims that wait for `claim_gate` now."""
        self.claim_requests: list[str | None] = []
        """The request id of each claim, in order."""
        self.claim_errors: list[Exception] = []
        """The next claims raise these, in order, as if their answers were lost."""

    async def health(self) -> dict[str, Any]:
        return {"workflow": "demo", "checksum": self.checksum}

    async def claim(
        self,
        supervisor: str,
        states: Collection[str],
        *,
        stop: asyncio.Event | None = None,
        request: str | None = None,
    ) -> TaskGrant | None:
        self.calls.append(("claim", supervisor, tuple(states)))
        self.claim_requests.append(request)
        if self.claim_errors:
            raise self.claim_errors.pop(0)
        if self.claim_gate is not None:
            self.claim_waits += 1
            try:
                await self.claim_gate.wait()
            finally:
                self.claim_waits -= 1
        return self.grants.pop(0) if self.grants else None

    async def report(self, task: int, outcome: Outcome, *, supervisor: str) -> None:
        self.calls.append(("report", task, outcome.kind))
        self.attempts.append(("report", task, outcome.attempt))
        outcome = replace(outcome, attempt=None)
        if self.refusals:
            raise self.refusals.pop(0)
        if self.refuse_reports:
            raise Refused(409, f"task {task} is not this supervisor's")
        self.reports[task] = outcome

    async def heartbeat(self, supervisor: str, tasks: Collection[int]) -> list[int]:
        self.calls.append(("heartbeat", tuple(sorted(tasks))))
        return sorted(self.lost & set(tasks))

    async def note(self, task: int, text: str, *, attempt: int) -> None:
        self.attempts.append(("note", task, attempt))
        await asyncio.sleep(self.note_delay)
        if self.refuse_notes:
            raise Refused(409, f"task {task} is not this supervisor's")
        self.calls.append(("note", task, text))

    async def set_memory(self, task: int, key: str, value: Any, *, attempt: int) -> None:
        self.attempts.append(("memory", task, attempt))
        self.calls.append(("memory", task, key, value))

    def reported(self, task: int) -> bool:
        return task in self.reports

    def called(self, *call: Any) -> bool:
        return call in self.calls

    def claims(self) -> int:
        return sum(1 for call in self.calls if call[0] == "claim")

    def sends(self, task: int) -> list[tuple[Any, ...]]:
        """The memory writes, notes and reports of `task`, in order."""
        kinds = ("memory", "note", "report")
        return [call for call in self.calls if call[0] in kinds and call[1] == task]


def grant(
    task: int,
    state: str,
    *,
    token: str = "T-1",
    attempt: int = 1,
    memory: dict[str, Any] | None = None,
) -> TaskGrant:
    return TaskGrant(
        task=task,
        token=token,
        state=state,
        visit=1,
        attempt=attempt,
        context={"repo": "acme/api"},
        memory=memory or {},
        entered_by=None,
    )


def make(api: FakeApi, *routines: Routine, tasks: int = 4, cancel_wait: float = 0.5) -> Supervisor:
    module = ModuleType("routines")
    for number, routine in enumerate(routines):
        setattr(module, f"routine_{number}", routine)
    return Supervisor(
        api,
        Registry.from_modules([module]),
        name="box-1",
        tasks=tasks,
        heartbeat_every=0.05,
        idle=0.01,
        cancel_wait=cancel_wait,
    )


def nothing_runs(supervisor: Supervisor) -> bool:
    return not supervisor.running


def runs(supervisor: Supervisor, task: int) -> bool:
    return task in supervisor.running


async def until(condition: Callable[..., bool], *args: Any, timeout: float = 5.0) -> None:
    """Wait until `condition(*args)` is true."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition(*args):
        assert loop.time() < deadline, f"timed out waiting for {condition.__name__}{args}"
        await asyncio.sleep(0.01)


async def run_until(
    supervisor: Supervisor, condition: Callable[..., bool], *args: Any, signals: int = 1
) -> int:
    """Run the supervisor until `condition(*args)`, then send `signals` stop signals.
    Returns the exit code of `run`."""
    running = asyncio.create_task(supervisor.run())
    await until(condition, *args)
    for _ in range(signals):
        supervisor.on_signal()
    return await asyncio.wait_for(running, 5.0)
