"""What crosses the network between the control plane and a supervisor, as JSON.

Both sides use these types, so a grant or an outcome means the same thing on
each side. The JSON keys are the field names. `None` fields are left out.
`from_json` refuses data of the wrong shape with `WireError`, one plain sentence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Self, cast

from stateous.model.durations import duration_problem

OutcomeKind = Literal["event", "waiting", "failed"]
_KINDS: tuple[OutcomeKind, ...] = ("event", "waiting", "failed")


class WireError(ValueError):
    """A message that does not have the shape the protocol needs."""


@dataclass(frozen=True)
class EventRecord:
    """An event that happened to a token, with its payload as JSON."""

    name: str
    payload: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.payload is not None:
            out["payload"] = self.payload
        return out

    @classmethod
    def from_json(cls, data: Any) -> Self:
        what = "an event record"
        body = _object(data, what)
        return cls(_string(body, "name", what), _optional_object(body, "payload", what))


@dataclass(frozen=True)
class TaskGrant:
    """A claimed task: "do state S for token T", with all the routine may read."""

    task: int
    token: str
    state: str
    visit: int
    attempt: int
    context: dict[str, Any]
    memory: dict[str, Any]
    entered_by: EventRecord | None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "task": self.task,
            "token": self.token,
            "state": self.state,
            "visit": self.visit,
            "attempt": self.attempt,
            "context": self.context,
            "memory": self.memory,
        }
        if self.entered_by is not None:
            out["entered_by"] = self.entered_by.to_json()
        return out

    @classmethod
    def from_json(cls, data: Any) -> Self:
        what = "a task grant"
        body = _object(data, what)
        entered = body.get("entered_by")
        return cls(
            task=_whole(body, "task", what),
            token=_string(body, "token", what),
            state=_string(body, "state", what),
            visit=_whole(body, "visit", what),
            attempt=_whole(body, "attempt", what),
            context=_required_object(body, "context", what),
            memory=_required_object(body, "memory", what),
            entered_by=None if entered is None else EventRecord.from_json(entered),
        )


@dataclass(frozen=True)
class Schedule:
    """A timer a routine set: the clock fires `event` on the token `after` from now,
    if the token is still in the state."""

    event: str
    payload: dict[str, Any] | None
    after: str
    """A duration: '30s', '10m', '6h', '1d'."""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"event": self.event}
        if self.payload is not None:
            out["payload"] = self.payload
        out["after"] = self.after
        return out

    @classmethod
    def from_json(cls, data: Any) -> Self:
        what = "a schedule"
        body = _object(data, what)
        event = _string(body, "event", what)
        after = _string(body, "after", what)
        problem = duration_problem(after)
        if problem is not None:
            raise WireError(f"a schedule's 'after': {problem}")
        return cls(event, _optional_object(body, "payload", what), after)


@dataclass(frozen=True)
class Outcome:
    """What a supervisor reports for a task: an event, waiting, or a failure, with
    the routine's context writes and timers."""

    kind: OutcomeKind
    event: str | None = None
    """When `kind` is "event"."""
    payload: dict[str, Any] | None = None
    error: str | None = None
    """When `kind` is "failed"."""
    context: dict[str, Any] = field(default_factory=dict[str, Any])
    """The routine's writes."""
    schedules: tuple[Schedule, ...] = ()
    attempt: int | None = None
    """The attempt of the task that the report is for. The control plane refuses
    the report when the task is on another attempt. None: any attempt."""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.event is not None:
            out["event"] = self.event
        if self.payload is not None:
            out["payload"] = self.payload
        if self.error is not None:
            out["error"] = self.error
        out["context"] = self.context
        out["schedules"] = [schedule.to_json() for schedule in self.schedules]
        if self.attempt is not None:
            out["attempt"] = self.attempt
        return out

    @classmethod
    def from_json(cls, data: Any) -> Self:
        what = "an outcome"
        body = _object(data, what)
        kind = body.get("kind")
        if kind not in _KINDS:
            raise WireError(
                f"an outcome's 'kind' must be 'event', 'waiting' or 'failed', not {kind!r}"
            )
        event = _string(body, "event", "an 'event' outcome") if kind == "event" else None
        error = _string(body, "error", "a 'failed' outcome") if kind == "failed" else None
        schedules: Any = body.get("schedules", [])
        if not isinstance(schedules, list):
            raise WireError("an outcome's 'schedules' must be a list")
        attempt = None if body.get("attempt") is None else _whole(body, "attempt", what)
        if attempt is not None and attempt < 1:
            raise WireError("an outcome's 'attempt' is 1 or more")
        return cls(
            kind=kind,
            event=event,
            payload=_optional_object(body, "payload", what),
            error=error,
            context=_optional_object(body, "context", what) or {},
            schedules=tuple(Schedule.from_json(item) for item in cast("list[Any]", schedules)),
            attempt=attempt,
        )


def _object(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise WireError(f"{what} must be a JSON object, not {type(data).__name__}")
    return cast("dict[str, Any]", data)


def _string(body: dict[str, Any], key: str, what: str) -> str:
    value = body.get(key)
    if not isinstance(value, str):
        raise WireError(f"{what} needs {key!r} as a string")
    return value


def _whole(body: dict[str, Any], key: str, what: str) -> int:
    value = body.get(key)
    if type(value) is not int:
        raise WireError(f"{what} needs {key!r} as a whole number")
    return value


def _optional_object(body: dict[str, Any], key: str, what: str) -> dict[str, Any] | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise WireError(f"{what}: {key!r} must be a JSON object or null")
    return cast("dict[str, Any]", value)


def _required_object(body: dict[str, Any], key: str, what: str) -> dict[str, Any]:
    value = _optional_object(body, key, what)
    if value is None:
        raise WireError(f"{what} needs {key!r} as a JSON object")
    return value
