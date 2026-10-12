"""The engine: tokens, events and tasks of one compiled workflow.

Each public call is one transaction. The ledger entries and the rows they explain
(the token, its tasks, its timers, its memory) commit together, or not at all. When
Postgres stops a call for a deadlock, the call runs once more (`_retried`).

How a call ends:
- A malformed request raises and records nothing: an unknown token or task, or a
  payload or a context that breaks its schema.
- An event that cannot move the token is recorded as `event_rejected`. Then the
  call raises `EventNotAllowed` or `TokenNotActive`.
- All else is recorded, and the call returns.

A token starts at START and leaves it at once, on `__started__`, along the one
transition whose guard holds on its starting context. When it enters END, it is
completed. It keeps the state it came from, so the history shows how it ended.

A parallel state's work is its child workflows (see the guide, "Nested and
parallel work"). Entering it
starts one child token for each, in the same transaction. When a child ends, the
same transaction checks the parent's join, and fires `joined` on the parent when
the join is met. When the parent leaves the state, its live children are cancelled.
A child has the lock of its top token, so a parent and its children change together.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from collections.abc import Callable, Collection, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Literal, ParamSpec, TypeVar, cast

from sqlalchemy.exc import DBAPIError, IntegrityError

from stateous.control.graph import (
    END,
    ERROR,
    JOINED,
    OWN_EVENTS,
    START,
    STARTED,
    TIMEOUT,
    Graph,
    TransitionSpec,
)
from stateous.control.store import Store, Transaction, tables
from stateous.model.durations import duration_problem, parse_duration
from stateous.model.guards import GuardInput, evaluate
from stateous.model.wire import EventRecord, Outcome, Schedule, TaskGrant
from stateous.model.workflow import MAX_TOKEN_ID

log = logging.getLogger("stateous.engine")

DEFAULT_LEASE = timedelta(minutes=2)
"""How long a claimed task stays with its supervisor without a heartbeat."""

ENDED = ("completed", "failed", "cancelled")
"""A token in one of these statuses never moves again."""

TOKEN_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
"""A token id: a letter or digit, then up to 127 letters, digits, `.`, `_` or `-`."""
KEY_LENGTH = 256
NOTE_LENGTH = 500
MEMORY_KEY_LENGTH = 128
SUPERVISOR_LENGTH = 256
REQUEST_PATTERN = r"[A-Za-z0-9._-]{1,64}"
"""A claim's request id: 1 to 64 letters, digits, `.`, `_` or `-`."""
MOST_TOKENS = 1000
"""The most tokens that one listing returns."""
WORKED_ON = ("claimed", "waiting")
"""A task in one of these statuses is still worked on: by its supervisor, or outside."""
TRY_AGAIN = frozenset({"40P01", "40001"})
"""The SQLSTATEs of a deadlock and of a serialization failure. Postgres stops one
transaction of a deadlock; a public call that it stops runs once more."""

_P = ParamSpec("_P")
_R = TypeVar("_R")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _retried(call: Callable[_P, _R]) -> Callable[_P, _R]:
    """Run a public call once more when the database stops it for a deadlock or a
    serialization failure. Its transaction rolled back, so nothing is applied twice.
    Any other error, and a second deadlock, go to the caller."""

    @functools.wraps(call)
    def once_more(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return call(*args, **kwargs)
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) not in TRY_AGAIN:
                raise
            log.warning("the database stopped %s for a deadlock; trying once more", call.__name__)
        return call(*args, **kwargs)

    return once_more


class EngineError(Exception):
    """The base of every error that the engine raises for a request. `code` is the
    stable code of the refusal (see the guide, "The HTTP API"): the API sends it,
    and clients read it, never the sentence."""

    code: ClassVar[str] = "unexpected"


class TokenNotFound(EngineError, LookupError):
    """No token has this id."""

    code: ClassVar[str] = "token_not_found"


class TaskNotFound(EngineError, LookupError):
    """No task has this id."""

    code: ClassVar[str] = "task_not_found"


class InvalidContext(EngineError, ValueError):
    """A context, or a routine's changes to it, breaks the workflow's context schema;
    or no transition from START holds on the starting context."""

    code: ClassVar[str] = "invalid_context"


class InvalidPayload(EngineError, ValueError):
    """A payload breaks its event's schema; or a schedule, a note, a memory key or
    value, or an idempotency key is not valid."""

    code: ClassVar[str] = "invalid_payload"


class InvalidArgument(EngineError, ValueError):
    """A supervisor name, a claim's request id or a listing limit is out of its range;
    or a report's `attempt` argument and its outcome's `attempt` differ."""

    code: ClassVar[str] = "invalid_argument"


class InvalidTokenId(EngineError, ValueError):
    """A token id does not match the allowed pattern, or differs only in case from
    an existing one."""

    code: ClassVar[str] = "invalid_token_id"


class EventNotAllowed(EngineError, ValueError):
    """The event cannot move the token: the workflow does not declare it, it is
    Stateous's own, the state has no transition on it, or no guard holds."""

    code: ClassVar[str] = "event_not_allowed"


class TokenNotActive(EngineError, ValueError):
    """The token is in a status that does not allow the request."""

    code: ClassVar[str] = "token_not_active"


class TaskNotHeld(EngineError, ValueError):
    """The task is not this supervisor's, or no one works on it now (or on the
    attempt that the request names)."""

    code: ClassVar[str] = "task_not_held"


class AttemptOver(TaskNotHeld):
    """The request is for an attempt of the task that is over: the task moved to a
    new attempt. It is a `TaskNotHeld`, so `except TaskNotHeld` also catches it."""

    code: ClassVar[str] = "attempt_over"


class ChildToken(EngineError, ValueError):
    """The token is a child of a parallel state: it is part of its parent's work. It
    cannot be paused, resumed or cancelled on its own; steer its top token."""

    code: ClassVar[str] = "child_token"


@dataclass(frozen=True)
class Token:
    token: str
    state: str
    status: str
    visit: int
    context: dict[str, Any]
    entered_at: datetime
    parent: str | None = None
    """The token whose parallel state started this one; None for a top token."""


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
        self.graphs: dict[str, Graph] = {graph.name: graph}
        """The main workflow and each child workflow, by name."""
        for name in graph.workflows:
            self.graphs[name] = graph.child(name)

    # ── tokens ──────────────────────────────────────────────────────────

    @_retried
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
                row = self._create(tx, token, context, self.graph)
            return _token(row)

    @_retried
    def get(self, token: str) -> Token:
        with self._transaction() as tx:
            return _token(self._existing(tx, token))

    @_retried
    def tokens(
        self, *, status: str | None = None, limit: int = 100, parent: str | None = None
    ) -> list[Token]:
        """Tokens, the most recently moved first. With `parent`: only its children."""
        if not 0 < limit <= MOST_TOKENS:
            raise InvalidArgument(f"a listing returns 1 to {MOST_TOKENS} tokens, not {limit}")
        with self._transaction() as tx:
            return [_token(row) for row in tx.find_tokens(status, limit, parent=parent)]

    @_retried
    def history(self, token: str) -> list[Entry]:
        with self._transaction() as tx:
            self._existing(tx, token)
            return [_entry(entry) for entry in tx.entries(token)]

    # ── steering ────────────────────────────────────────────────────────

    @_retried
    def pause(self, token: str) -> Token:
        """Stop a token: it does not move, its queued task is not handed out, its
        timers stand still. Events that arrive meanwhile are held, not lost. Its
        children are paused too."""
        with self._transaction() as tx:
            row = self._existing(tx, token)
            self._refuse_child(tx, row, "pause")
            if row.status != "active":
                raise TokenNotActive(
                    f"token {token!r} is {row.status}; only an active token can be paused"
                )
            self._pause(tx, row)
            return _token(row)

    def _pause(self, tx: Transaction, row: tables.Token) -> None:
        row.status = "paused"
        tx.append(row.token, "paused")
        for child in self._children(tx, row):
            if child.status == "active":
                self._pause(tx, child)

    @_retried
    def resume(self, token: str) -> Token:
        """Let a paused token move again, and its children too. Their timers get the
        paused time back. Then the events that the token and its children held apply
        in the order they arrived; those that no longer fit are dropped and recorded.
        A child's event can meet a join, and move the parent."""
        with self._transaction() as tx:
            row = self._existing(tx, token)
            self._refuse_child(tx, row, "resume")
            if row.status != "paused":
                raise TokenNotActive(
                    f"token {token!r} is {row.status}; only a paused token can be resumed"
                )
            self._resume(tx, row)
            return _token(row)

    def _resume(self, tx: Transaction, row: tables.Token) -> None:
        """Resume the token and each paused token below it. Then apply what they all
        held, as one list in the order of arrival (see the guide, "Pause and
        resume")."""
        held: list[tuple[_Held, tables.Token]] = []
        for member in self._family(tx, row):
            if member.status != "paused":
                continue
            entries = tx.entries(member.token)
            paused_at = max((e.at for e in entries if e.kind == "paused"), default=tx.now)
            for timer in tx.pending_timers(member.token):
                # Only the part of the pause after the timer was set.
                timer.due_at += tx.now - max(paused_at, timer.set_at)
            member.status = "active"
            tx.append(member.token, "resumed")
            held.extend((event, member) for event in _held_events(entries))
        for event, member in sorted(held, key=_arrival):
            self._replay(tx, member, event)

    def _family(self, tx: Transaction, row: tables.Token) -> list[tables.Token]:
        """The token, then each child of its current state, and their children, depth
        first."""
        found = [row]
        for child in self._children(tx, row):
            found.extend(self._family(tx, child))
        return found

    def _refuse_child(
        self, tx: Transaction, row: tables.Token, verb: Literal["pause", "resume", "cancel"]
    ) -> None:
        """A child is part of its parent's work (see the guide, "Steer the parent"):
        steer the top token instead. `ChildToken` names it."""
        if row.parent is None:
            return
        top = row
        while top.parent is not None and (above := tx.token(top.parent)) is not None:
            top = above
        done = {"pause": "paused", "resume": "resumed", "cancel": "cancelled"}[verb]
        raise ChildToken(
            f"token {row.token!r} is a child of {row.parent!r}: it is part of its parent's "
            f"work, and cannot be {done} on its own; {verb} {top.token!r} instead"
        )

    @_retried
    def cancel(self, token: str) -> Token:
        """Stop a token for good: its tasks and timers are cancelled, its children too,
        its memory is deleted, and anything it held is dropped."""
        with self._transaction() as tx:
            row = self._existing(tx, token)
            self._refuse_child(tx, row, "cancel")
            if row.status not in ("active", "paused"):
                raise TokenNotActive(
                    f"token {token!r} is {row.status}; only an active or paused token "
                    "can be cancelled"
                )
            self._cancel(tx, row)
            return _token(row)

    def _cancel(self, tx: Transaction, row: tables.Token, **data: Any) -> None:
        """End a live token as cancelled. A paused token drops what it held. All that
        its state started stops, its children too. `data` goes in the ledger entry."""
        if row.status == "paused":
            for held in _held_events(tx.entries(row.token)):
                reason = "the token was cancelled"
                tx.append(row.token, "event_dropped", event=held.event, reason=reason)
        self._leave(tx, row, via=None, why="the parent was cancelled")
        self._end(tx, row, "cancelled", **data)

    def _replay(self, tx: Transaction, row: tables.Token, held: _Held) -> None:
        """Apply one held event, or drop it and say why. An event from a task applies
        only in the state and visit of that task, with its context changes and timers,
        in this transaction. A dropped event applies none of them. An own event
        (`error`) that cannot apply there fails the token: it has no task left to
        move it."""
        graph = self._graph(row)
        event, payload = held.event, held.payload
        if row.status != "active":
            reason = f"token {row.token!r} is {row.status}; it takes no events"
            tx.append(row.token, "event_dropped", event=event, reason=reason)
            return
        if held.origin is not None and held.origin != (row.state, row.visit):
            reason = "the token left the state it came from"
            tx.append(row.token, "event_dropped", event=event, reason=reason)
            return
        context = {**row.context, **held.context}
        if held.context and (errors := graph.context_errors(context)):
            reason = "the context changes break the schema: " + "; ".join(errors)
            tx.append(row.token, "event_dropped", event=event, reason=reason)
            return
        decision = self._decide(tx, row, event, payload, context)
        if isinstance(decision, _Refusal):
            tx.append(row.token, "event_dropped", event=event, reason=decision.reason)
            if decision.fail or event in OWN_EVENTS:
                self._fail(tx, row, decision.reason)
            return
        tx.append(row.token, "event_replayed", event=event)
        self._apply(tx, row, held.context, held.schedules, context)
        self._take(tx, row, decision, event, payload, key=None)

    # ── the graph on record, for serve ──────────────────────────────────

    @_retried
    def recorded_graph(self) -> RecordedGraph | None:
        """The structure and checksum on record; None for a new database."""
        with self._transaction() as tx:
            row = tx.control_plane()
            if row.structure is None or row.checksum is None:
                return None
            return RecordedGraph(row.structure, row.checksum)

    @_retried
    def live_tokens(self) -> int:
        """How many tokens are active or paused."""
        with self._transaction() as tx:
            return tx.count_live()

    @_retried
    def record_graph(self) -> None:
        """Put this engine's graph on record."""
        with self._transaction() as tx:
            row = tx.control_plane()
            row.structure, row.checksum = self.graph.structure, self.graph.checksum

    # ── events ──────────────────────────────────────────────────────────

    @_retried
    def fire(
        self, token: str, event: str, payload: dict[str, Any] | None, *, key: str | None = None
    ) -> Fired:
        """An event from outside: a person, a program, a webhook. With `key`, a second
        delivery of the same event is answered as a duplicate, and changes nothing."""
        if key is not None and not 0 < len(key) <= KEY_LENGTH:
            raise InvalidPayload(f"an idempotency key is 1 to {KEY_LENGTH} characters")
        if problem := _not_json(payload):
            raise InvalidPayload(f"the payload of {event!r} is not JSON: {problem}")
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
        graph = self._graph(row)
        if key is not None and tx.find_key(row.token, key) is not None:
            return Fired(_token(row), duplicate=True)
        if row.status in ENDED:
            reason = f"token {row.token!r} is {row.status}; it takes no events"
            return self._reject(tx, row, event, reason, TokenNotActive)
        if event in OWN_EVENTS:
            reason = f"{event!r} is Stateous's own event; only Stateous fires it"
            return self._reject(tx, row, event, reason, EventNotAllowed)
        if not graph.declares(event):
            reason = f"the workflow {graph.name!r} has no event {event!r}"
            return self._reject(tx, row, event, reason, EventNotAllowed)
        if errors := graph.payload_errors(event, payload):
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
        task: tables.Task | None = None,
    ) -> str | None:
        """Decide where one event leads, and record it: the heart of the engine. A
        paused token holds the event; an event from `task` is held with the state,
        visit and task it came from. Returns why the event cannot move the token, or
        None. The caller records the refusal."""
        if row.status in ENDED:
            return f"token {row.token!r} is {row.status}; it takes no events"
        if row.status == "paused":
            self._hold(tx, row, event, payload, key=key, task=task)
            return None
        decision = self._decide(tx, row, event, payload, row.context)
        if isinstance(decision, _Refusal):
            if decision.fail:
                self._fail(tx, row, decision.reason)
                return None
            return decision.reason
        self._take(tx, row, decision, event, payload, key=key)
        return None

    def _hold(
        self,
        tx: Transaction,
        row: tables.Token,
        event: str,
        payload: dict[str, Any] | None,
        *,
        key: str | None,
        task: tables.Task | None,
        changes: dict[str, Any] | None = None,
        schedules: tuple[Schedule, ...] = (),
    ) -> None:
        """Hold an event for a paused token. An event from a task is bound to the
        task's state and visit: resume drops it if the token left them. A report's
        context changes and timers are held with its event. Until resume applies or
        drops them, this ledger entry is their only record. `arrival` counts the
        events that the whole family holds, so resume applies them in that order."""
        top = row
        while top.parent is not None:
            top = self._existing(tx, top.parent)
        arrival = 1 + sum(
            len(_held_events(tx.entries(member.token))) for member in self._family(tx, top)
        )
        if task is None:
            tx.append(
                row.token, "event_deferred", key=key, event=event, payload=payload, arrival=arrival
            )
            return
        held: dict[str, Any] = {}
        if changes:
            held["context"] = changes
        if schedules:
            held["schedules"] = [schedule.to_json() for schedule in schedules]
        tx.append(
            row.token, "event_deferred", key=key, event=event, payload=payload,
            state=task.state, visit=task.visit, task=task.id, arrival=arrival, **held,
        )  # fmt: skip

    def _decide(
        self,
        tx: Transaction,
        row: tables.Token,
        event: str,
        payload: dict[str, Any] | None,
        context: dict[str, Any],
    ) -> TransitionSpec | _Refusal:
        """The transition the event takes from the token's state, with this context;
        or why there is none. A transition into a parallel state whose children cannot
        start is refused. Changes nothing."""
        graph = self._graph(row)
        candidates = graph.candidates(row.state, event)
        if not candidates:
            return _Refusal(f"no transition for {event!r} from {row.state!r}")
        counts = tx.counts(row.token)
        counts[event] = counts.get(event, 0) + 1  # count() includes the event being routed
        branches = cast("list[dict[str, Any]]", (payload or {}).get("branches", []))
        facts = GuardInput(
            context=context,
            payload=payload or {},
            counts=counts,
            branches=branches if event == JOINED else [],
        )
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
        if chosen.target != END and (
            problem := self._cannot_enter(tx, row.token, graph, chosen.target, context)
        ):
            return _Refusal(problem)
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
        self._leave(tx, row, via=event, why=f"the parent left {row.state!r} on {event!r}")
        if chosen.target == END:
            self._end(tx, row, "completed")
        else:
            self._enter(tx, row, chosen.target)

    # ── tasks: the control plane's side of the supervisor ───────────────

    @_retried
    def claim(
        self, supervisor: str, states: Collection[str], *, request: str | None = None
    ) -> TaskGrant | None:
        """The oldest queued task of `states` whose token is active, now claimed by
        `supervisor` with a lease; None if there is none.

        With `request`: the control plane keeps this id, and what the claim got, as
        the supervisor's last claim. A repeat of that claim (the same id) gets the
        same grant again, while the supervisor still holds that attempt of the task:
        the lease is renewed, and no new task is claimed. So a claim whose answer
        was lost loses no task. Else the repeat is a new claim."""
        _check_supervisor(supervisor)
        if request is not None:
            _check_request(request)
        if not states and request is None:
            return None
        with self._transaction() as tx:
            if request is not None and (grant := self._repeat(tx, supervisor, request)):
                return grant
            task = tx.claim_task(supervisor, states) if states else None
            if request is not None:
                tx.remember_claim(supervisor, request, task)
            if task is None:
                return None
            task.lease_until = tx.now + self.lease
            tx.append(
                task.token, "task_claimed",
                task=task.id, supervisor=supervisor, attempt=task.attempt,
            )  # fmt: skip
            return self._grant(tx, task)

    def _repeat(self, tx: Transaction, supervisor: str, request: str) -> TaskGrant | None:
        """The grant of the supervisor's last claim, again, if `request` repeats it and
        the supervisor still holds that attempt of the task, with a lease that has
        not ended. The lease is renewed. None: the claim is a new one."""
        last = tx.last_claim(supervisor)
        if last.request != request or last.task_id is None:
            return None
        task = tx.task(last.task_id)
        if (
            task is None
            or task.status != "claimed"
            or task.claimed_by != supervisor
            or task.attempt != last.attempt
            or (task.lease_until is not None and task.lease_until < tx.now)
        ):
            return None
        task.lease_until = tx.now + self.lease
        return self._grant(tx, task)

    @_retried
    def report(
        self,
        task: int,
        outcome: Outcome,
        *,
        supervisor: str | None = None,
        attempt: int | None = None,
    ) -> Fired:
        """The outcome of a task. With `supervisor`: from the supervisor that claimed
        it. Without: from outside (CI, a person), for a claimed or waiting task, with
        an event or a failure. With `attempt` (or `outcome.attempt`): only for that
        attempt of the task, so a late report of an old attempt cannot land on a new
        one. The two must agree when both are given."""
        if supervisor is not None:
            _check_supervisor(supervisor)
        _check_outcome(outcome)
        if attempt is not None and outcome.attempt is not None and attempt != outcome.attempt:
            raise InvalidArgument(
                f"the attempt argument ({attempt}) and the outcome's attempt "
                f"({outcome.attempt}) differ"
            )
        attempt = outcome.attempt if attempt is None else attempt
        waiting = outcome.kind == "waiting"
        with self._transaction() as tx:
            held = self._worked_task(tx, task, supervisor, waiting=waiting, attempt=attempt)
            row = self._existing(tx, held.token)
            result = self._report(tx, row, held, outcome)
        return _settled(result)

    @_retried
    def heartbeat(self, supervisor: str, tasks: Collection[int]) -> list[int]:
        """Renew the leases of the tasks that `supervisor` still holds. Returns the
        others, sorted: the token left the state, was cancelled, or the lease ended.
        The supervisor stops the routines of those tasks."""
        _check_supervisor(supervisor)
        with self._transaction() as tx:
            renewed = tx.renew_leases(supervisor, tasks, tx.now + self.lease)
        return sorted(set(tasks) - renewed)

    @_retried
    def note(self, task: int, text: str, *, attempt: int | None = None) -> None:
        """One line of progress in the token's history. With `attempt`: only for that
        attempt of the task."""
        line = text.strip()
        if not 0 < len(line) <= NOTE_LENGTH:
            raise InvalidPayload(f"a note is 1 to {NOTE_LENGTH} characters")
        with self._transaction() as tx:
            held = self._worked_task(tx, task, None, waiting=False, attempt=attempt)
            tx.append(held.token, "note", task=held.id, text=line)

    @_retried
    def set_memory(self, task: int, key: str, value: Any, *, attempt: int | None = None) -> None:
        """Save one key of the routine memory of the task's token and state, at once.
        `None` deletes the key. The history records the key, never the value. With
        `attempt`: only for that attempt of the task."""
        if not 0 < len(key) <= MEMORY_KEY_LENGTH:
            raise InvalidPayload(f"a memory key is 1 to {MEMORY_KEY_LENGTH} characters")
        if value is not None:
            try:
                json.dumps(value, allow_nan=False)
            except (TypeError, ValueError) as error:
                raise InvalidPayload(f"the value of {key!r} is not JSON: {error}") from None
        with self._transaction() as tx:
            held = self._worked_task(tx, task, None, waiting=False, attempt=attempt)
            if value is None:
                if tx.forget(held.token, held.state, key):
                    tx.append(held.token, "memory_deleted", state=held.state, memory_key=key)
            else:
                tx.set_memory(held.token, held.state, key, value)
                tx.append(held.token, "memory_set", state=held.state, memory_key=key)

    def _worked_task(
        self,
        tx: Transaction,
        task_id: int,
        supervisor: str | None,
        *,
        waiting: bool,
        attempt: int | None,
    ) -> tables.Task:
        """The task, if it is still worked on, and, with `supervisor`, by it; and,
        with `attempt`, on that attempt."""
        task = tx.task(task_id)
        if task is None:
            raise TaskNotFound(f"there is no task {task_id}")
        if attempt is not None and attempt != task.attempt:
            # A request for an old attempt: that attempt is over. A request for an
            # attempt that did not start: no one works on that attempt.
            over = attempt < task.attempt
            refused = AttemptOver if over else TaskNotHeld
            raise refused(f"task {task_id} is at attempt {task.attempt}, not attempt {attempt}")
        if task.status not in WORKED_ON:
            raise TaskNotHeld(f"task {task_id} is {task.status}; no one works on it now")
        if supervisor is not None and (task.status != "claimed" or task.claimed_by != supervisor):
            raise TaskNotHeld(f"task {task_id} is not held by {supervisor!r}")
        if supervisor is None and waiting:
            raise TaskNotHeld("only the supervisor that holds a task reports it waiting")
        return task

    def _report(
        self, tx: Transaction, row: tables.Token, task: tables.Task, outcome: Outcome
    ) -> Fired | Exception:
        if outcome.kind == "failed":
            error = outcome.error or "the routine failed, and said no more"
            self._task_failed(tx, row, task, error)
            return Fired(_token(row))
        graph = self._graph(row)
        context = {**row.context, **outcome.context}
        if errors := graph.context_errors(context):
            raise InvalidContext("the context changes break the schema: " + "; ".join(errors))
        for schedule in outcome.schedules:
            if problem := self._schedule_problem(graph, schedule):
                raise InvalidPayload(problem)
        if outcome.kind == "waiting":
            self._apply(tx, row, outcome.context, outcome.schedules, context)
            task.status, task.lease_until = "waiting", None
            tx.append(row.token, "task_waiting", task=task.id)
            return Fired(_token(row))
        event = outcome.event or ""
        emits = graph.state(task.state).emits
        if event not in emits:
            allowed = ", ".join(emits) or "nothing"
            reason = f"{task.state!r} may not emit {event!r}; it may emit: {allowed}"
            return self._reject(tx, row, event, reason, EventNotAllowed)
        if errors := graph.payload_errors(event, outcome.payload):
            raise InvalidPayload(f"{event}: " + "; ".join(errors))
        # The guards read the context with the routine's changes. Decide first: a
        # refused outcome saves none of the changes. A paused token decides too, so
        # it refuses what an active token refuses, and holds only what can apply.
        decision = self._decide(tx, row, event, outcome.payload, context)
        if isinstance(decision, _Refusal) and not decision.fail:
            return self._reject(tx, row, event, decision.reason, EventNotAllowed)
        # No idempotency key here: that key space is for a caller's `fire`. A task
        # is reported once, since `_worked_task` refuses a second report on it.
        if row.status == "paused":
            # Hold the whole outcome: resume saves its changes with its event, or
            # saves none of them. The work is done.
            self._hold(
                tx, row, event, outcome.payload, key=None, task=task,
                changes=outcome.context, schedules=outcome.schedules,
            )  # fmt: skip
            task.status, task.lease_until = "done", None
            tx.append(row.token, "task_done", task=task.id)
            return Fired(_token(row))
        self._apply(tx, row, outcome.context, outcome.schedules, context)
        refusal = self._route(tx, row, event, outcome.payload, key=None, task=task)
        if refusal is not None:
            raise RuntimeError(f"the decision found a transition, but routing refused: {refusal}")
        return Fired(_token(row))

    def _apply(
        self,
        tx: Transaction,
        row: tables.Token,
        changes: dict[str, Any],
        schedules: tuple[Schedule, ...],
        context: dict[str, Any],
    ) -> None:
        """Save a report's context changes (`context` is the context with them), and
        set its timers. The timers are set before the outcome moves the token, so
        that leaving the state cancels them."""
        if changes:
            row.context = context
            tx.append(row.token, "context_changed", keys=sorted(changes))
        for schedule in schedules:
            after = parse_duration(schedule.after)
            self._set_timer(tx, row, schedule.event, schedule.payload, after, kind="timer")

    def _schedule_problem(self, graph: Graph, schedule: Schedule) -> str | None:
        """Why a routine's timer cannot be set on a token of `graph`, or None."""
        if problem := duration_problem(schedule.after):
            return f"a timer for {schedule.event!r}: {problem}"
        if schedule.event in OWN_EVENTS or not graph.declares(schedule.event):
            return f"a timer names {schedule.event!r}, which is not an event of {graph.name!r}"
        if errors := graph.payload_errors(schedule.event, schedule.payload):
            return f"a timer for {schedule.event!r}: " + "; ".join(errors)
        return None

    def _task_failed(
        self, tx: Transaction, row: tables.Token, task: tables.Task, error: str
    ) -> None:
        """One failed attempt. Queue the task again while the state's retries allow;
        then the token gets `error`. With no way to go on `error`, the token fails."""
        graph = self._graph(row)
        tx.append(row.token, "task_failed", task=task.id, error=error)
        task.lease_until = None
        if task.attempt <= graph.state(task.state).retries:
            task.status, task.claimed_by = "queued", None
            tx.append(row.token, "task_retrying", task=task.id, attempt=task.attempt + 1)
            # The next attempt runs the routine again, and it sets its own timers.
            # The state's timeout stays: it covers every attempt.
            for timer in tx.pending_timers(row.token):
                if timer.kind == "timer":
                    timer.status = "cancelled"
            return
        task.status = "failed"
        if row.status == "paused":
            # A paused token never moves or ends on its own. Hold `error`, as `_route`
            # would; resume applies it, and fails the token if it still does not fit.
            self._hold(tx, row, ERROR, None, key=None, task=task)
        elif not graph.candidates(row.state, ERROR):
            self._fail(tx, row, f"{task.state!r} failed, and has no transition on 'error': {error}")
        elif (refusal := self._route(tx, row, ERROR, None, key=None)) is not None:
            self._fail(tx, row, f"{task.state!r} failed, and 'error' cannot move it: {refusal}")

    def _grant(self, tx: Transaction, task: tables.Task) -> TaskGrant:
        """The grant of a claimed task. A child's state is named `<workflow>/<state>`,
        as the claim names it."""
        row = self._existing(tx, task.token)
        last = tx.last_accepted(task.token)
        entered_by = (
            None if last is None else EventRecord(last.data["event"], last.data.get("payload"))
        )
        state = task.state if row.workflow is None else f"{row.workflow}/{task.state}"
        return TaskGrant(
            task=task.id,
            token=task.token,
            state=state,
            visit=task.visit,
            attempt=task.attempt,
            context=dict(row.context),
            memory=tx.memory(task.token, task.state),
            entered_by=entered_by,
        )

    # ── time ────────────────────────────────────────────────────────────

    @_retried
    def tick(self) -> int:
        """What the clock does: end the leases of tasks whose supervisors stopped
        answering, then fire each timer that is due (a timeout is one). Each in its
        own transaction, which looks again: a token that moved meanwhile is left
        alone. Returns how many leases ended and timers fired."""
        return self._end_leases() + self._fire_timers()

    def _end_leases(self) -> int:
        """A task whose lease ended counts as a failed attempt."""
        with self._transaction() as tx:
            expired = tx.expired_leases()
        ended = 0
        for task_id in expired:
            with self._transaction() as tx:
                task = tx.task(task_id)
                if task is None or task.status != "claimed":
                    continue  # it finished meanwhile
                if not tx.end_lease(task_id):
                    continue  # a heartbeat renewed it meanwhile
                row = self._existing(tx, task.token)
                who, span = task.claimed_by, _span(self.lease)
                error = f"{who} stopped answering: no heartbeat for {span}"
                self._task_failed(tx, row, task, error)
                ended += 1
        return ended

    def _fire_timers(self) -> int:
        """Fire each due timer as its event, from the clock, routed as any other."""
        with self._transaction() as tx:
            due = tx.due_timers()
        fired = 0
        for timer_id in due:
            with self._transaction() as tx:
                timer = tx.timer(timer_id)
                if timer is None or timer.status != "pending" or timer.due_at > tx.now:
                    continue  # cancelled, or moved by a resume, meanwhile
                row = self._existing(tx, timer.token)
                if row.status != "active" or (row.state, row.visit) != (timer.state, timer.visit):
                    continue
                timer.status = "fired"
                tx.append(row.token, "timer_fired", event=timer.event)
                refusal = self._route(tx, row, timer.event, timer.payload, key=None)
                if refusal is not None:
                    if timer.kind == "timeout":
                        self._fail(tx, row, f"{row.state!r} timed out: {refusal}")
                    else:
                        tx.append(row.token, "event_rejected", event=timer.event, reason=refusal)
                fired += 1
        return fired

    # ── entering, leaving, ending ───────────────────────────────────────

    def _create(
        self,
        tx: Transaction,
        token: str,
        context: dict[str, Any],
        graph: Graph,
        *,
        parent: tables.Token | None = None,
    ) -> tables.Token:
        """A new token of `graph`'s workflow, in the state that its context leads to.
        With `parent`, it is a child of the parent's parallel state, and it takes the
        lock of the parent's family."""
        values = graph.with_defaults(context)
        if problem := _not_json(values):
            raise InvalidContext(f"the context is not JSON: {problem}")
        if errors := graph.context_errors(values):
            raise InvalidContext(f"{graph.name}: " + "; ".join(errors))
        chosen = self._start_transition(graph, values)
        if chosen.target != END and (
            problem := self._cannot_enter(tx, token, graph, chosen.target, values)
        ):
            raise InvalidContext(problem)
        if parent is not None:
            tx.adopt(token, parent.token)
        family: dict[str, str] = {} if parent is None else {"parent": parent.token}
        tx.append(token, "started", context=values, **family)
        row = tables.Token(
            token=token, state=START, status="active", visit=0, context=values,
            entered_at=tx.now,
            parent=None if parent is None else parent.token,
            workflow=None if parent is None else graph.name,
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

    def _start_transition(self, graph: Graph, context: dict[str, Any]) -> TransitionSpec:
        """The transition from START that a token of `graph` with this context takes.
        Its guards read only the context: nothing has happened yet."""
        facts = GuardInput(context=context, payload={}, counts={})
        starts = graph.starts
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

    def _cannot_enter(
        self, tx: Transaction, token: str, graph: Graph, state: str, context: dict[str, Any]
    ) -> str | None:
        """Why `token` cannot enter `state` of `graph` with this context now: a child
        that the state would start cannot start. None: it can. These are the checks
        that `_create` makes, made before anything is recorded, down through nested
        parallel states."""
        spec = graph.state(state)
        if not spec.parallel:
            return None
        visit = tx.visits(token, state) + 1
        inputs = {key: context[key] for key in spec.inputs if key in context}
        for workflow in spec.parallel:
            child = child_id(token, state, visit, workflow)
            if len(child) > MAX_TOKEN_ID:
                return (
                    f"the child {child!r} of {state!r} would have an id longer than "
                    f"{MAX_TOKEN_ID} characters; use a shorter token id"
                )
            if tx.token_taken(child):
                return f"the child id {child!r} is taken by another token"
            if problem := self._cannot_start(tx, child, self.graphs[workflow], inputs):
                return f"the child {workflow!r} of {state!r} cannot start: {problem}"
        return None

    def _cannot_start(
        self, tx: Transaction, token: str, graph: Graph, context: dict[str, Any]
    ) -> str | None:
        """Why a new token `token` of `graph` cannot start with this context; or None."""
        values = graph.with_defaults(context)
        if errors := graph.context_errors(values):
            return "its context breaks the schema: " + "; ".join(errors)
        try:
            chosen = self._start_transition(graph, values)
        except InvalidContext as error:
            return str(error)
        if chosen.target == END:
            return None
        return self._cannot_enter(tx, token, graph, chosen.target, values)

    def _enter(self, tx: Transaction, row: tables.Token, state: str) -> None:
        """Enter `state`: queue its task, or, for a parallel state, start one child for
        each of its workflows. Set its timeout, if it has one, before the children start."""
        spec = self._graph(row).state(state)
        visit = tx.visits(row.token, state) + 1
        tx.append(row.token, "entered", state=state, visit=visit)
        row.state, row.visit, row.entered_at = state, visit, tx.now
        if not spec.parallel:
            task = tx.queue_task(row.token, state, visit)
            tx.append(row.token, "task_queued", task=task.id, state=state)
        if spec.timeout is not None:
            self._set_timer(tx, row, TIMEOUT, None, parse_duration(spec.timeout), kind="timeout")
        inputs = {key: row.context[key] for key in spec.inputs if key in row.context}
        for workflow in spec.parallel:
            if (row.status, row.state, row.visit) != ("active", state, visit):
                # A child of a hand-made graph file can end as it starts, and meet the
                # join. Then the parent has left the state: it starts no more children.
                return
            child = child_id(row.token, state, visit, workflow)
            tx.append(row.token, "branch_started", workflow=workflow, token=child)
            self._create(tx, child, inputs, self.graphs[workflow], parent=row)

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

    def _leave(self, tx: Transaction, row: tables.Token, *, via: str | None, why: str) -> None:
        """Stop all that the current state started: its tasks, its timers and its
        children. When the token leaves on one of the state's own outcomes (`via` in
        its emits), a task that a supervisor or someone outside works on is done. Else
        it is cancelled, so the supervisor's heartbeat finds it lost. Each live child
        is cancelled, with `why` as its reason."""
        finished = via is not None and via in self._graph(row).state(row.state).emits
        for task in tx.open_tasks(row.token):
            done = finished and task.status in ("claimed", "waiting")
            task.status = "done" if done else "cancelled"
            task.lease_until = None
            tx.append(row.token, "task_done" if done else "task_cancelled", task=task.id)
        for timer in tx.pending_timers(row.token):
            timer.status = "cancelled"
        for child in self._children(tx, row):
            if child.status in ("active", "paused"):
                self._cancel(tx, child, reason=why)

    def _end(
        self,
        tx: Transaction,
        row: tables.Token,
        status: Literal["completed", "failed", "cancelled"],
        **data: Any,
    ) -> None:
        """The token ends. Its routine memory goes in the same transaction. A child
        that completes or fails tells its parent, whose join may now be met. A child
        is cancelled only when its parent leaves the state, so that tells no one."""
        row.status = status
        tx.append(row.token, status, **data)
        tx.delete_memory(row.token)
        if status != "cancelled":
            self._branch_finished(tx, row)

    def _fail(self, tx: Transaction, row: tables.Token, reason: str) -> None:
        """No way to go on: the token fails, in the state it is in, and says why."""
        self._leave(tx, row, via=None, why=f"the parent failed: {reason}")
        self._end(tx, row, "failed", reason=reason)

    def _children(self, tx: Transaction, row: tables.Token) -> list[tables.Token]:
        """The children that the token's current visit of a parallel state started, in
        the order of the state's workflows. Empty in any other state."""
        if row.state in (START, END):
            return []
        spec = self._graph(row).state(row.state)
        if not spec.parallel:
            return []
        found = {child.token: child for child in tx.children(row.token)}
        ids = [child_id(row.token, row.state, row.visit, workflow) for workflow in spec.parallel]
        return [found[i] for i in ids if i in found]

    def _branch_finished(self, tx: Transaction, child: tables.Token) -> None:
        """A child ended: fire `joined` on its parent when the parent's join is met.
        With `all`, the payload has an entry for each child; with `any`, for this one.
        A join that no transition takes fails the parent: it cannot go on."""
        if child.parent is None:
            return
        parent = tx.token(child.parent)
        if parent is None or parent.status != "active":
            return  # a paused parent's children are paused too: they do not end
        if parent.state in (START, END):
            return
        spec = self._graph(parent).state(parent.state)
        prefix = child_id(parent.token, parent.state, parent.visit, "")
        if not spec.parallel or not child.token.startswith(prefix):
            return  # the parent has left the state that started this child
        ended = [sibling for sibling in self._children(tx, parent) if sibling.status in ENDED]
        if spec.join == "all" and len(ended) < len(spec.parallel):
            return
        chosen = ended if spec.join == "all" else [child]
        payload = {"branches": [self._branch(tx, sibling) for sibling in chosen]}
        refusal = self._route(tx, parent, JOINED, payload, key=None)
        if refusal is not None:
            self._fail(tx, parent, f"the join of {parent.state!r} cannot move the token: {refusal}")

    def _branch(self, tx: Transaction, child: tables.Token) -> dict[str, Any]:
        """One entry of the `joined` payload. `final` and `payload` tell of one moment:
        the event that brought the child into its last state."""
        return {
            "workflow": child.workflow,
            "token": child.token,
            "status": child.status,
            "final": child.state,
            "payload": tx.arrival_payload(child.token),
        }

    # ── small parts ─────────────────────────────────────────────────────

    def _graph(self, row: tables.Token) -> Graph:
        """The graph of the token's own workflow: a child routes by its child workflow,
        a top token by the main one."""
        return self.graph if row.workflow is None else self.graphs[row.workflow]

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
        """One transaction, at the clock's time. Two calls under different family locks
        (a top token's start and a child's entry) can make ids that differ only in
        case at the same time. The database refuses the second: `InvalidTokenId`."""
        try:
            with self.store.transaction(self.clock()) as tx:
                yield tx
        except IntegrityError as error:
            if "tokens_folded" not in str(error.orig):
                raise
            raise InvalidTokenId(
                "a token id differs only in case from one that another call made at the "
                "same time; token ids are compared without regard to case"
            ) from None


def _token(row: tables.Token) -> Token:
    return Token(
        token=row.token,
        state=row.state,
        status=row.status,
        visit=row.visit,
        context=dict(row.context),
        entered_at=row.entered_at.replace(tzinfo=UTC),
        parent=row.parent,
    )


def child_id(parent: str, state: str, visit: int, workflow: str) -> str:
    """The id of the child that `parent` starts for `workflow` on its visit `visit` of
    the parallel state `state`: `TICKET-1.reviews.1.design-review` (see the guide,
    "Child tokens")."""
    return f"{parent}.{state}.{visit}.{workflow}"


def _entry(row: tables.LedgerEntry) -> Entry:
    return Entry(seq=row.seq, at=row.at.replace(tzinfo=UTC), kind=row.kind, data=dict(row.data))


def _settled(result: Fired | Exception) -> Fired:
    """Raise a refusal after its transaction committed; or return the result."""
    if isinstance(result, Exception):
        raise result
    return result


def _not_json(value: Any) -> str | None:
    """Why a value cannot be stored as JSON (NaN and Infinity are not JSON); or None."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        return str(error)
    return None


def _check_outcome(outcome: Outcome) -> None:
    """Refuse an outcome with a value that is not JSON: in its context changes, its
    payload, or the payload of one of its timers."""
    if problem := _not_json(outcome.context):
        raise InvalidContext(f"the context changes are not JSON: {problem}")
    if problem := _not_json(outcome.payload):
        raise InvalidPayload(f"the payload of {outcome.event!r} is not JSON: {problem}")
    for schedule in outcome.schedules:
        if problem := _not_json(schedule.payload):
            raise InvalidPayload(f"a timer for {schedule.event!r} is not JSON: {problem}")


def _check_supervisor(supervisor: str) -> None:
    if not 0 < len(supervisor) <= SUPERVISOR_LENGTH:
        raise InvalidArgument(f"a supervisor name is 1 to {SUPERVISOR_LENGTH} characters")


def _check_request(request: str) -> None:
    if not re.fullmatch(REQUEST_PATTERN, request):
        raise InvalidArgument("a claim's request id is 1 to 64 letters, digits, '.', '_' or '-'")


def _iso(moment: datetime) -> str:
    """A stored moment (UTC, no zone) as ISO 8601 with `Z`."""
    return moment.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _span(span: timedelta) -> str:
    """'2 minutes', '30 seconds'."""
    seconds = int(span.total_seconds())
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    return f"{seconds} second{'' if seconds == 1 else 's'}"


@dataclass(frozen=True)
class _Held:
    """An event that a paused token holds."""

    event: str
    payload: dict[str, Any] | None
    origin: tuple[str, int] | None
    """The state and visit of the task the event came from; None for an event from
    outside, which applies in any state."""
    context: dict[str, Any]
    """The context changes of a held outcome; empty for an event from outside."""
    schedules: tuple[Schedule, ...]
    """The timers of a held outcome. Resume sets them: `after` counts from then."""
    arrival: int
    """Its place among the events that the token's family holds: 1, 2, 3 …; 0 for an
    event held before Stateous counted them."""
    at: datetime
    token: str
    seq: int


def _arrival(item: tuple[_Held, tables.Token]) -> tuple[int, datetime, str, int]:
    """The order in which a family's held events apply: the order they arrived."""
    held = item[0]
    return (held.arrival, held.at, held.token, held.seq)


def _schedules(data: list[Any] | None) -> tuple[Schedule, ...]:
    """The timers of a held outcome, as its ledger entry records them."""
    return tuple(Schedule.from_json(item) for item in data or [])


def _held_events(entries: list[tables.LedgerEntry]) -> list[_Held]:
    """The events a paused token holds: those deferred since it was last paused.
    Each resume settles all of them, so nothing older is still held."""
    since = max((e.seq for e in entries if e.kind == "paused"), default=0)
    return [
        _Held(
            event=e.data["event"],
            payload=e.data.get("payload"),
            origin=(e.data["state"], e.data["visit"]) if "state" in e.data else None,
            context=dict(e.data.get("context") or {}),
            schedules=_schedules(e.data.get("schedules")),
            arrival=int(e.data.get("arrival", 0)),
            at=e.at,
            token=e.token,
            seq=e.seq,
        )
        for e in entries
        if e.kind == "event_deferred" and e.seq > since
    ]
