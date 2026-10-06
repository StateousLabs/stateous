"""The engine: tokens, events and tasks of one compiled workflow.

Each public call is one transaction. The ledger entries and the rows they explain
(the token, its tasks, its timers, its memory) commit together, or not at all.

How a call ends:
- A malformed request raises and records nothing: an unknown token or task, or a
  payload or a context that breaks its schema.
- An event that cannot move the token is recorded as `event_rejected`. Then the
  call raises `EventNotAllowed` or `TokenNotActive`.
- All else is recorded, and the call returns.

A token starts at START and leaves it at once, on `__started__`, along the one
transition whose guard holds on its starting context. When it enters END, it is
completed. It keeps the state it came from, so the history shows how it ended.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from stateous.control.graph import (
    END,
    OWN_EVENTS,
    START,
    STARTED,
    TIMEOUT,
    Graph,
    TransitionSpec,
)
from stateous.control.store import Store, Transaction, tables
from stateous.model.durations import parse_duration
from stateous.model.guards import GuardInput, evaluate

DEFAULT_LEASE = timedelta(minutes=2)
"""How long a claimed task stays with its supervisor without a heartbeat."""

ENDED = ("completed", "failed", "cancelled")
"""A token in one of these statuses never moves again."""

TOKEN_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
"""A token id: a letter or digit, then up to 127 letters, digits, `.`, `_` or `-`."""
KEY_LENGTH = 256


def utcnow() -> datetime:
    return datetime.now(UTC)


class TokenNotFound(LookupError):
    """No token has this id."""


class TaskNotFound(LookupError):
    """No task has this id."""


class InvalidContext(ValueError):
    """A context, or a routine's changes to it, breaks the workflow's context schema;
    or no transition from START holds on the starting context."""


class InvalidPayload(ValueError):
    """A payload breaks its event's schema; or a schedule, a note, a memory key or
    value, or an idempotency key is not valid."""


class InvalidTokenId(ValueError):
    """A token id does not match the allowed pattern, or differs only in case from
    an existing one."""


class EventNotAllowed(ValueError):
    """The event cannot move the token: the workflow does not declare it, it is
    Stateous's own, the state has no transition on it, or no guard holds."""


class TokenNotActive(ValueError):
    """The token is in a status that does not allow the request."""


class TaskNotHeld(ValueError):
    """The task is not this supervisor's, or no one works on it any more."""


@dataclass(frozen=True)
class Token:
    token: str
    state: str
    status: str
    visit: int
    context: dict[str, Any]
    entered_at: datetime


@dataclass(frozen=True)
class Entry:
    seq: int
    at: datetime
    kind: str
    data: dict[str, Any]


@dataclass(frozen=True)
class Fired:
    token: Token
    duplicate: bool = False


@dataclass(frozen=True)
class RecordedGraph:
    """The graph that the database has on record (`Engine.record_graph`)."""

    structure: str
    checksum: str


@dataclass(frozen=True)
class _Refusal:
    """Why an event cannot move a token."""

    reason: str
    fail: bool = False
    """True: the token cannot go on at all (two guards hold). It fails."""


class Engine:
    """Routes the events of one compiled workflow, over one store."""

    def __init__(
        self,
        store: Store,
        graph: Graph,
        *,
        clock: Callable[[], datetime] = utcnow,
        lease: timedelta = DEFAULT_LEASE,
    ) -> None:
        self.store = store
        self.graph = graph
        self.clock = clock
        """The time of each transaction. Tests give a clock that they move by hand."""
        self.lease = lease

    # ── tokens ──────────────────────────────────────────────────────────

    def start(self, token: str, context: dict[str, Any]) -> Token:
        """Start a token: it leaves START for the state its context leads to. To
        start a token that exists returns it, unchanged."""
        if not re.fullmatch(TOKEN_ID_PATTERN, token):
            raise InvalidTokenId(
                "a token id is a letter or digit, then up to 127 letters, digits, '.', '_' or '-'"
            )
        with self._transaction() as tx:
            row = tx.token(token)
            if row is None:
                if (taken := tx.token_folded(token)) is not None:
                    raise InvalidTokenId(
                        f"a token {taken!r} already exists; token ids are compared "
                        "without regard to case"
                    )
                row = self._create(tx, token, context)
            return _token(row)

    def get(self, token: str) -> Token:
        with self._transaction() as tx:
            return _token(self._existing(tx, token))

    def tokens(self, *, status: str | None = None, limit: int = 100) -> list[Token]:
        """Tokens, the most recently moved first."""
        with self._transaction() as tx:
            return [_token(row) for row in tx.find_tokens(status, limit)]

    def history(self, token: str) -> list[Entry]:
        with self._transaction() as tx:
            self._existing(tx, token)
            return [_entry(entry) for entry in tx.entries(token)]

    # ── the graph on record, for serve ──────────────────────────────────

    def recorded_graph(self) -> RecordedGraph | None:
        """The structure and checksum on record; None for a new database."""
        with self._transaction() as tx:
            row = tx.control_plane()
            if row.structure is None or row.checksum is None:
                return None
            return RecordedGraph(row.structure, row.checksum)

    def live_tokens(self) -> int:
        """How many tokens are active or paused."""
        with self._transaction() as tx:
            return tx.count_live()

    def record_graph(self) -> None:
        """Put this engine's graph on record."""
        with self._transaction() as tx:
            row = tx.control_plane()
            row.structure, row.checksum = self.graph.structure, self.graph.checksum

    # ── events ──────────────────────────────────────────────────────────

    def fire(
        self, token: str, event: str, payload: dict[str, Any] | None, *, key: str | None = None
    ) -> Fired:
        """An event from outside: a person, a program, a webhook. With `key`, a second
        delivery of the same event is answered as a duplicate, and changes nothing."""
        if key is not None and not 0 < len(key) <= KEY_LENGTH:
            raise InvalidPayload(f"an idempotency key is 1 to {KEY_LENGTH} characters")
        with self._transaction() as tx:
            row = self._existing(tx, token)
            result = self._fire(tx, row, event, payload, key)
        return _settled(result)

    def _fire(
        self,
        tx: Transaction,
        row: tables.Token,
        event: str,
        payload: dict[str, Any] | None,
        key: str | None,
    ) -> Fired | Exception:
        if key is not None and tx.find_key(row.token, key) is not None:
            return Fired(_token(row), duplicate=True)
        if row.status in ENDED:
            reason = f"token {row.token!r} is {row.status}; it takes no events"
            return self._reject(tx, row, event, reason, TokenNotActive)
        if event in OWN_EVENTS:
            reason = f"{event!r} is Stateous's own event; only Stateous fires it"
            return self._reject(tx, row, event, reason, EventNotAllowed)
        if not self.graph.declares(event):
            reason = f"the workflow {self.graph.name!r} has no event {event!r}"
            return self._reject(tx, row, event, reason, EventNotAllowed)
        if errors := self.graph.payload_errors(event, payload):
            raise InvalidPayload(f"{event}: " + "; ".join(errors))
        if (refusal := self._route(tx, row, event, payload, key=key)) is not None:
            return self._reject(tx, row, event, refusal, EventNotAllowed)
        return Fired(_token(row))

    # ── routing ─────────────────────────────────────────────────────────

    def _route(
        self,
        tx: Transaction,
        row: tables.Token,
        event: str,
        payload: dict[str, Any] | None,
        *,
        key: str | None,
    ) -> str | None:
        """Decide where one event leads, and record it: the heart of the engine. A
        paused token holds the event. Returns why the event cannot move the token, or
        None. The caller records the refusal."""
        if row.status in ENDED:
            return f"token {row.token!r} is {row.status}; it takes no events"
        if row.status == "paused":
            tx.append(row.token, "event_deferred", key=key, event=event, payload=payload)
            return None
        decision = self._decide(tx, row, event, payload, row.context)
        if isinstance(decision, _Refusal):
            if decision.fail:
                self._fail(tx, row, decision.reason)
                return None
            return decision.reason
        self._take(tx, row, decision, event, payload, key=key)
        return None

    def _decide(
        self,
        tx: Transaction,
        row: tables.Token,
        event: str,
        payload: dict[str, Any] | None,
        context: dict[str, Any],
    ) -> TransitionSpec | _Refusal:
        """The transition the event takes from the token's state, with this context;
        or why there is none. Changes nothing."""
        candidates = self.graph.candidates(row.state, event)
        if not candidates:
            return _Refusal(f"no transition for {event!r} from {row.state!r}")
        counts = tx.counts(row.token)
        counts[event] = counts.get(event, 0) + 1  # count() includes the event being routed
        facts = GuardInput(context=context, payload=payload or {}, counts=counts)
        matching = [c for c in candidates if c.when is not None and evaluate(c.when, facts)]
        if len(matching) > 1:
            # The compiler refuses guards that can both hold, so this is a graph file
            # from before that check. Never a silent choice: the token fails.
            targets = ", ".join(repr(c.target) for c in matching)
            return _Refusal(
                f"{event!r} from {row.state!r} matched more than one guard ({targets}); "
                "compile the workflow again, which refuses such guards",
                fail=True,
            )
        chosen = matching[0] if matching else next((c for c in candidates if c.when is None), None)
        if chosen is None:
            return _Refusal(f"no guard matched {event!r} from {row.state!r}, and no otherwise")
        return chosen

    def _take(
        self,
        tx: Transaction,
        row: tables.Token,
        chosen: TransitionSpec,
        event: str,
        payload: dict[str, Any] | None,
        *,
        key: str | None,
    ) -> None:
        """Move the token along the chosen transition, and record it."""
        tx.append(
            row.token, "event_accepted", key=key, event=event, payload=payload, state=row.state
        )
        self._leave(tx, row, via=event)
        if chosen.target == END:
            self._end(tx, row, "completed")
        else:
            self._enter(tx, row, chosen.target)

    # ── entering, leaving, ending ───────────────────────────────────────

    def _create(self, tx: Transaction, token: str, context: dict[str, Any]) -> tables.Token:
        values = self.graph.with_defaults(context)
        if errors := self.graph.context_errors(values):
            raise InvalidContext(f"{self.graph.name}: " + "; ".join(errors))
        chosen = self._start_transition(values)
        tx.append(token, "started", context=values)
        row = tables.Token(
            token=token, state=START, status="active", visit=0, context=values,
            entered_at=tx.now,
        )  # fmt: skip
        tx.add_token(row)
        # Recorded as any other move: the history shows where the token began, and the
        # first state's routine is told that `__started__` brought the token there.
        tx.append(token, "event_accepted", event=STARTED, payload=None, state=START)
        if chosen.target == END:
            self._end(tx, row, "completed")
        else:
            self._enter(tx, row, chosen.target)
        return row

    def _start_transition(self, context: dict[str, Any]) -> TransitionSpec:
        """The transition from START that a token with this context takes. Its guards
        read only the context: nothing has happened yet."""
        facts = GuardInput(context=context, payload={}, counts={})
        starts = self.graph.starts
        matching = [t for t in starts if t.when is not None and evaluate(t.when, facts)]
        if len(matching) > 1:
            targets = ", ".join(repr(t.target) for t in matching)
            raise InvalidContext(
                f"more than one transition from START holds ({targets}); compile the "
                "workflow again, which refuses such guards"
            )
        chosen = matching[0] if matching else next((t for t in starts if t.when is None), None)
        if chosen is None:
            raise InvalidContext(
                "no transition from START holds on this context, and there is no otherwise"
            )
        return chosen

    def _enter(self, tx: Transaction, row: tables.Token, state: str) -> None:
        """Enter `state`: queue its task, and set its timeout, if it has one."""
        spec = self.graph.state(state)
        visit = tx.visits(row.token, state) + 1
        tx.append(row.token, "entered", state=state, visit=visit)
        row.state, row.visit, row.entered_at = state, visit, tx.now
        task = tx.queue_task(row.token, state, visit)
        tx.append(row.token, "task_queued", task=task.id, state=state)
        if spec.timeout is not None:
            self._set_timer(tx, row, TIMEOUT, None, parse_duration(spec.timeout), kind="timeout")

    def _set_timer(
        self,
        tx: Transaction,
        row: tables.Token,
        event: str,
        payload: dict[str, Any] | None,
        after: timedelta,
        *,
        kind: Literal["timeout", "timer"],
    ) -> None:
        """A timer in the token's current state and visit. Leaving the state cancels it."""
        timer = tx.add_timer(
            row.token, row.state, row.visit, event, payload, tx.now + after, kind=kind
        )
        tx.append(row.token, "timer_set", event=event, due_at=_iso(timer.due_at))

    def _leave(self, tx: Transaction, row: tables.Token, *, via: str | None) -> None:
        """Stop all that the current state started: its tasks and its timers. When
        the token leaves on one of the state's own outcomes (`via` in its emits), a
        task that a supervisor or someone outside works on is done. Else it is
        cancelled, so the supervisor's heartbeat finds it lost."""
        finished = via is not None and via in self.graph.state(row.state).emits
        for task in tx.open_tasks(row.token):
            done = finished and task.status in ("claimed", "waiting")
            task.status = "done" if done else "cancelled"
            task.lease_until = None
            tx.append(row.token, "task_done" if done else "task_cancelled", task=task.id)
        for timer in tx.pending_timers(row.token):
            timer.status = "cancelled"

    def _end(
        self,
        tx: Transaction,
        row: tables.Token,
        status: Literal["completed", "failed", "cancelled"],
        **data: Any,
    ) -> None:
        """The token ends. Its routine memory goes in the same transaction."""
        row.status = status
        tx.append(row.token, status, **data)
        tx.delete_memory(row.token)

    def _fail(self, tx: Transaction, row: tables.Token, reason: str) -> None:
        """No way to go on: the token fails, in the state it is in, and says why."""
        self._leave(tx, row, via=None)
        self._end(tx, row, "failed", reason=reason)

    # ── small parts ─────────────────────────────────────────────────────

    def _reject(
        self,
        tx: Transaction,
        row: tables.Token,
        event: str,
        reason: str,
        error: type[ValueError],
    ) -> ValueError:
        """Record a refused event. Returns the error to raise after the commit."""
        tx.append(row.token, "event_rejected", event=event, reason=reason)
        return error(reason)

    def _existing(self, tx: Transaction, token: str) -> tables.Token:
        row = tx.token(token)
        if row is None:
            raise TokenNotFound(f"there is no token {token!r}")
        return row

    @contextmanager
    def _transaction(self) -> Generator[Transaction]:
        with self.store.transaction(self.clock()) as tx:
            yield tx


def _token(row: tables.Token) -> Token:
    return Token(
        token=row.token,
        state=row.state,
        status=row.status,
        visit=row.visit,
        context=dict(row.context),
        entered_at=row.entered_at.replace(tzinfo=UTC),
    )


def _entry(row: tables.LedgerEntry) -> Entry:
    return Entry(seq=row.seq, at=row.at.replace(tzinfo=UTC), kind=row.kind, data=dict(row.data))


def _settled(result: Fired | Exception) -> Fired:
    """Raise a refusal after its transaction committed; or return the result."""
    if isinstance(result, Exception):
        raise result
    return result


def _iso(moment: datetime) -> str:
    """A stored moment (UTC, no zone) as ISO 8601 with `Z`."""
    return moment.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
