"""The report of a try-out: why it stopped, each step, and the history of each token."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from stateous.control.engine import HistoryEntry, Token
from stateous.tryout.watch import Step

Reason = Literal[
    "completed",
    "failed",
    "cancelled",
    "no_routine",
    "waits_outside",
    "waits_timer",
    "quiet",
    "time_limit",
]

PAGE_OF: dict[str, str | None] = {
    "completed": None,
    "failed": "concepts/tasks-and-routines.md",
    "cancelled": "how-to/steer-tokens.md",
    "no_routine": "how-to/run-supervisors.md",
    "waits_outside": "concepts/tasks-and-routines.md",
    "waits_timer": "concepts/timers.md",
    "quiet": "concepts/reliability.md",
    "time_limit": "concepts/agents.md",
}
"""For each reason of a stop, the reference page that tells about it."""


@dataclass(frozen=True)
class Stop:
    """Why the try-out stopped, at which token and state, in one sentence."""

    reason: Reason
    token: str
    state: str
    sentence: str
    page: str | None


@dataclass(frozen=True)
class TryReport:
    """All that the agent needs after a try-out. It never needs to open the database."""

    stop: Stop
    steps: list[Step]
    tokens: list[Token]
    """The top token, then each child token."""
    histories: dict[str, list[HistoryEntry]]
    fires_used: list[str]
    fires_unused: list[str]
    database: Path
    """The SQLite file of the try-out. It is kept."""

    def to_text(self) -> str:
        lines = [self.stop.sentence, "", "tokens:"]
        for token in self.tokens:
            context = json.dumps(token.context, sort_keys=True)
            lines.append(f"{token.token}  {token.status} at {token.state}  context {context}")
        lines += ["", "steps:"]
        for step in self.steps:
            took = "running" if step.ended is None else f"{step.seconds()} s  {step.outcome}"
            lines.append(f"{step.token}  {step.state}  attempt {step.attempt}  {took}")
            lines += [f"    note: {note}" for note in step.notes]
        for token, entries in self.histories.items():
            lines += ["", f"history of {token}:"]
            for entry in entries:
                data = json.dumps(entry.data, sort_keys=True, default=str)
                lines.append(f"{entry.seq}  {entry.kind}  {entry.at.isoformat()}  {data}")
        lines.append("")
        if self.fires_used:
            lines.append(f"fires used: {', '.join(self.fires_used)}")
        if self.fires_unused:
            lines.append(f"fires not used: {', '.join(self.fires_unused)}")
        lines.append(f"database: {self.database}")
        if self.stop.page is not None:
            lines.append(f"read: references/{self.stop.page}")
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "stop": {
                "reason": self.stop.reason,
                "token": self.stop.token,
                "state": self.stop.state,
                "sentence": self.stop.sentence,
                "page": None if self.stop.page is None else f"references/{self.stop.page}",
            },
            "steps": [_step(step) for step in self.steps],
            "tokens": [_token(token) for token in self.tokens],
            "histories": {
                token: [_entry(entry) for entry in entries]
                for token, entries in self.histories.items()
            },
            "fires_used": list(self.fires_used),
            "fires_unused": list(self.fires_unused),
            "database": str(self.database),
        }


def _time(at: datetime | None) -> str | None:
    return None if at is None else at.isoformat()


def _plain(value: Any) -> Any:
    """`value` with only JSON types: a datetime or another object becomes text."""
    return json.loads(json.dumps(value, default=str))


def _step(step: Step) -> dict[str, Any]:
    return {
        "task": step.task,
        "token": step.token,
        "state": step.state,
        "attempt": step.attempt,
        "started": _time(step.started),
        "ended": _time(step.ended),
        "seconds": step.seconds(),
        "outcome": step.outcome,
        "notes": list(step.notes),
    }


def _token(token: Token) -> dict[str, Any]:
    return {
        "token": token.token,
        "state": token.state,
        "status": token.status,
        "visit": token.visit,
        "parent": token.parent,
        "context": _plain(token.context),
        "entered_at": _time(token.entered_at),
    }


def _entry(entry: HistoryEntry) -> dict[str, Any]:
    return {"seq": entry.seq, "at": _time(entry.at), "kind": entry.kind, "data": _plain(entry.data)}
