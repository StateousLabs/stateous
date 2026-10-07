"""What a routine gets as `ctx` with a task.

Reads come from the task's grant. Writes to the token's context, and timers the
routine sets, are kept here and travel back with the report: they are saved with
the outcome, or not at all. Memory and notes are different: they are sent at
once, through the callables the supervisor passes in, so they survive a crash.

A value read is a copy, and a value written is kept as JSON keeps it (a tuple
becomes a list). So a change made in place, such as `ctx["items"].append(2)`,
changes nothing: write the new value with `ctx["items"] = ...`.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from stateous.model.durations import duration_problem
from stateous.model.events import Occurrence
from stateous.model.wire import EventRecord, Schedule, TaskGrant

C = TypeVar("C", bound=BaseModel)

MAX_NOTE = 500
"""The longest note, in characters."""

MAX_MEMORY_KEY = 128
"""The longest memory key, in characters (the store's `memory.key` column)."""


class Memory(MutableMapping[str, Any]):
    """The routine's own memory, for this token and this state (design section 6.2).

    Each change is saved at once: `save(key, value)`, or `save(key, None)` for a
    deletion. Values must be JSON."""

    def __init__(self, values: Mapping[str, Any], save: Callable[[str, Any], None]) -> None:
        self._values = dict(values)
        self._save = save

    def __getitem__(self, key: str) -> Any:
        return copy.deepcopy(self._values[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __setitem__(self, key: str, value: Any) -> None:
        _check_key(key)
        if value is None:
            raise ValueError(
                f"memory[{key!r}] = None: use `del ctx.memory[{key!r}]` to delete a key"
            )
        kept = _as_json(f"memory[{key!r}]", value)
        self._save(key, copy.deepcopy(kept))
        self._values[key] = kept

    def __delitem__(self, key: str) -> None:
        if key not in self._values:
            raise KeyError(key)
        self._save(key, None)
        del self._values[key]

    def __repr__(self) -> str:
        return f"Memory({self._values!r})"


class Context(Generic[C]):
    """A token's context `C`, as a routine sees it.

    A routine gets one as `ctx`, typed by the generated contract (e.g.
    `TicketContext = Context[Ticket]`). `ctx["key"]` reads the token's context, and
    `ctx["key"] = value` writes it; the control plane checks the writes against the
    workflow's schema and saves them with the outcome."""

    def __init__(
        self,
        grant: TaskGrant,
        *,
        save_memory: Callable[[str, Any], None],
        note: Callable[[str], None],
    ) -> None:
        self.token: str = grant.token
        """The token's id."""
        self.task: int = grant.task
        """The task's id."""
        self.visit: int = grant.visit
        """1 the first time the token is in this state, 2 the next time, and so on."""
        self.attempt: int = grant.attempt
        """1, then 2 after a retry."""
        self.entered_by: EventRecord | None = grant.entered_by
        """The event that brought the token here, with its payload."""
        self.memory: Memory = Memory(grant.memory, save_memory)
        """The routine's own memory, for this token and this state."""
        self._values: dict[str, Any] = dict(grant.context)
        self._writes: dict[str, Any] = {}
        self._schedules: list[Schedule] = []
        self._note = note

    def __getitem__(self, key: str) -> Any:
        if key not in self._values:
            raise KeyError(f"the token's context has no {key!r}")
        return copy.deepcopy(self._values[key])

    def __setitem__(self, key: str, value: Any) -> None:
        kept = _as_json(f"ctx[{key!r}]", value)
        self._values[key] = kept
        self._writes[key] = copy.deepcopy(kept)

    def get(self, key: str, default: Any = None) -> Any:
        """A copy of the token's context value at `key`, or `default` if it is not set."""
        return copy.deepcopy(self._values[key]) if key in self._values else default

    def note(self, text: str) -> None:
        """One line of progress in the token's history, sent at once. Empty text (after
        stripping) does nothing: no record, no send, no error."""
        line = text.strip()
        if not line:
            return
        if len(line) > MAX_NOTE:
            raise ValueError(f"a note is at most {MAX_NOTE} characters; this one has {len(line)}")
        self._note(line)

    def schedule(self, event: type[Occurrence[Any]] | Occurrence[Any], *, after: str) -> None:
        """A timer: the clock fires `event` on this token `after` from now ('30s',
        '10m', '6h', '1d'), if the token is still in this state. It is set when the
        report is saved."""
        problem = duration_problem(after)
        if problem is not None:
            raise ValueError(problem)
        if isinstance(event, Occurrence):
            name, payload = event.name, event.payload
        else:
            if event.payload_model() is not None:
                raise ValueError(
                    f"{event.name!r} carries a payload; schedule an occurrence with "
                    "its payload, not the event class"
                )
            name, payload = event.name, None
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else None
        self._schedules.append(Schedule(event=name, payload=data, after=after))

    def writes(self) -> dict[str, Any]:
        """The keys this routine set, with their values; sent with the report."""
        return copy.deepcopy(self._writes)

    def schedules(self) -> list[Schedule]:
        """The timers this routine set; sent with the report."""
        return list(self._schedules)


def _check_key(key: str) -> None:
    is_text = isinstance(key, str)  # pyright: ignore[reportUnnecessaryIsInstance]
    if not is_text or not 1 <= len(key) <= MAX_MEMORY_KEY:
        raise ValueError(f"a memory key is 1 to {MAX_MEMORY_KEY} characters, not {key!r}")


def _as_json(what: str, value: Any) -> Any:
    """`value` as JSON keeps it: a copy that shares nothing with `value`."""
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise TypeError(f"{what} must be JSON: {error}") from None
