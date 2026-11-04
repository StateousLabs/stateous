"""`Watching`: a transport that passes each call to another one, and records the tasks.

The try-out puts it between the supervisor and the control plane. So the report can
tell each task: its state, its attempt, how long it took, its outcome and its notes.
"""

from __future__ import annotations

import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from stateous.model.wire import Outcome, TaskGrant
from stateous.transport import Refused, Transport


@dataclass
class Step:
    """One attempt of one task, as the supervisor did it. It changes while it runs."""

    task: int
    token: str
    state: str
    attempt: int
    started: datetime
    ended: datetime | None = None
    outcome: str | None = None
    """`event <name>`, `waiting`, `failed: <error>` or `refused: <message>`. None while
    it runs."""
    notes: list[str] = field(default_factory=list[str])

    def seconds(self) -> float | None:
        """How long the attempt took. None while it runs."""
        if self.ended is None:
            return None
        return round((self.ended - self.started).total_seconds(), 1)


class Watching:
    """A `Transport` that passes each call to `inner`, and records the tasks."""

    def __init__(self, inner: Transport) -> None:
        self.inner = inner
        self.where = inner.where
        self.steps: list[Step] = []
        self.served: set[str] = set()
        """The states that the supervisor claims tasks of."""
        self.last_change = time.monotonic()
        """When the work last moved: a grant, a report, a heartbeat, a note or a memory."""

    def running(self) -> list[Step]:
        """The steps that have no outcome yet."""
        return [step for step in self.steps if step.ended is None]

    def _changed(self) -> None:
        self.last_change = time.monotonic()

    def _step(self, task: int, attempt: int | None) -> Step | None:
        for step in reversed(self.steps):
            if step.task == task and step.ended is None and attempt in (None, step.attempt):
                return step
        return None

    async def health(self) -> dict[str, Any]:
        return await self.inner.health()

    async def claim(
        self,
        supervisor: str,
        states: Collection[str],
        *,
        request: str | None = None,
        wait: float = 0,
        labels: Mapping[str, str] | None = None,
    ) -> TaskGrant | None:
        self.served.update(states)
        grant = await self.inner.claim(
            supervisor, states, request=request, wait=wait, labels=labels
        )
        if grant is not None:
            self._changed()
            started = datetime.now(UTC)
            self.steps.append(Step(grant.task, grant.token, grant.state, grant.attempt, started))
        return grant

    async def report(self, task: int, outcome: Outcome, *, supervisor: str) -> None:
        step = self._step(task, outcome.attempt)
        try:
            await self.inner.report(task, outcome, supervisor=supervisor)
        except Refused as refusal:
            self._close(step, f"refused: {refusal.message}")
            raise
        self._close(step, _outcome_text(outcome))

    def _close(self, step: Step | None, text: str) -> None:
        self._changed()
        if step is not None:
            step.ended = datetime.now(UTC)
            step.outcome = text

    async def heartbeat(self, supervisor: str, tasks: Collection[int]) -> list[int]:
        self._changed()
        return await self.inner.heartbeat(supervisor, tasks)

    async def note(self, task: int, text: str, *, attempt: int) -> None:
        self._changed()
        step = self._step(task, attempt)
        if step is not None:
            step.notes.append(text)
        await self.inner.note(task, text, attempt=attempt)

    async def set_memory(self, task: int, key: str, value: Any, *, attempt: int) -> None:
        self._changed()
        await self.inner.set_memory(task, key, value, attempt=attempt)

    async def aclose(self) -> None:
        await self.inner.aclose()


def _outcome_text(outcome: Outcome) -> str:
    if outcome.kind == "event":
        return f"event {outcome.event}"
    if outcome.kind == "failed":
        return f"failed: {outcome.error}"
    return "waiting"
