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

import json
import re
from collections.abc import Callable, Collection, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from stateous.control.graph import (
    END,
    ERROR,
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
MOST_TOKENS = 1000
"""The most tokens that one listing returns."""
WORKED_ON = ("claimed", "waiting")
"""A task in one of these statuses is still worked on: by its supervisor, or outside."""


def utcnow() -> datetime:
    return datetime.now(UTC)


class EngineError(Exception):
    """The base of every error that the engine raises for a request."""


class TokenNotFound(EngineError, LookupError):
    """No token has this id."""


class TaskNotFound(EngineError, LookupError):
    """No task has this id."""


class InvalidContext(EngineError, ValueError):
    """A context, or a routine's changes to it, breaks the workflow's context schema;
    or no transition from START holds on the starting context."""


class InvalidPayload(EngineError, ValueError):
    """A payload breaks its event's schema; or a schedule, a note, a memory key or
    value, or an idempotency key is not valid."""


class InvalidArgument(EngineError, ValueError):
    """A supervisor name or a listing limit is out of its range."""


class InvalidTokenId(EngineError, ValueError):
    """A token id does not match the allowed pattern, or differs only in case from
    an existing one."""


class EventNotAllowed(EngineError, ValueError):
    """The event cannot move the token: the workflow does not declare it, it is
    Stateous's own, the state has no transition on it, or no guard holds."""


class TokenNotActive(EngineError, ValueError):
    """The token is in a status that does not allow the request."""


class TaskNotHeld(EngineError, ValueError):
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
        if not 0 < limit <= MOST_TOKENS:
            raise InvalidArgument(f"a listing returns 1 to {MOST_TOKENS} tokens, not {limit}")
        with self._transaction() as tx:
            return [_token(row) for row in tx.find_tokens(status, limit)]

    def history(self, token: str) -> list[Entry]:
        with self._transaction() as tx:
            self._existing(tx, token)
            return [_entry(entry) for entry in tx.entries(token)]

    # ── steering ────────────────────────────────────────────────────────

    def pause(self, token: str) -> Token:
        """Stop a token: it does not move, its queued task is not handed out, its
        timers stand still. Events that arrive meanwhile are held, not lost."""
        with self._transaction() as tx:
            row = self._existing(tx, token)
            if row.status != "active":
                raise TokenNotActive(
                    f"token {token!r} is {row.status}; only an active token can be paused"
                )
            row.status = "paused"
            tx.append(token, "paused")
            return _token(row)

    def resume(self, token: str) -> Token:
        """Let a paused token move again. Its timers get the paused time back. The
        held events apply in the order they arrived; those that no longer fit are
        dropped and recorded."""
        with self._transaction() as tx:
            row = self._existing(tx, token)
            if row.status != "paused":
                raise TokenNotActive(
                    f"token {token!r} is {row.status}; only a paused token can be resumed"
                )
            entries = tx.entries(token)
            paused_at = max((e.at for e in entries if e.kind == "paused"), default=tx.now)
            for timer in tx.pending_timers(token):
                # Only the part of the pause after the timer was set.
                timer.due_at += tx.now - max(paused_at, timer.set_at)
            row.status = "active"
            tx.append(token, "resumed")
            for held in _held_events(entries):
                self._replay(tx, row, held)
            return _token(row)

    def cancel(self, token: str) -> Token:
        """Stop a token for good: its tasks and timers are cancelled, its memory is
        deleted, and anything it held is dropped."""
        with self._transaction() as tx:
            row = self._existing(tx, token)
            if row.status not in ("active", "paused"):
                raise TokenNotActive(
                    f"token {token!r} is {row.status}; only an active or paused token "
                    "can be cancelled"
                )
            if row.status == "paused":
                for held in _held_events(tx.entries(token)):
                    reason = "the token was cancelled"
                    tx.append(token, "event_dropped", event=held.event, reason=reason)
            self._leave(tx, row, via=None)
            self._end(tx, row, "cancelled")
            return _token(row)

    def _replay(self, tx: Transaction, row: tables.Token, held: _Held) -> None:
        """Apply one held event, or drop it and say why. An event from a task applies
        only in the state and visit of that task. An own event (`error`) that cannot
        apply there fails the token: it has no task left to move it."""
        event, payload = held.event, held.payload
        if row.status != "active":
            reason = f"token {row.token!r} is {row.status}; it takes no events"
            tx.append(row.token, "event_dropped", event=event, reason=reason)
            return
        if held.origin is not None and held.origin != (row.state, row.visit):
            reason = "the token left the state it came from"
            tx.append(row.token, "event_dropped", event=event, reason=reason)
            return
        decision = self._decide(tx, row, event, payload, row.context)
        if isinstance(decision, _Refusal):
            tx.append(row.token, "event_dropped", event=event, reason=decision.reason)
            if decision.fail or event in OWN_EVENTS:
                self._fail(tx, row, decision.reason)
            return
        tx.append(row.token, "event_replayed", event=event)
        self._take(tx, row, decision, event, payload, key=None)

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
    ) -> None:
        """Hold an event for a paused token. An event from a task is bound to the
        task's state and visit: resume drops it if the token left them."""
        if task is None:
            tx.append(row.token, "event_deferred", key=key, event=event, payload=payload)
        else:
            tx.append(
                row.token, "event_deferred", key=key, event=event, payload=payload,
                state=task.state, visit=task.visit, task=task.id,
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

    # ── tasks: the control plane's side of the supervisor ───────────────

    def claim(self, supervisor: str, states: Collection[str]) -> TaskGrant | None:
        """The oldest queued task of `states` whose token is active, now claimed by
        `supervisor` with a lease; None if there is none."""
        _check_supervisor(supervisor)
        if not states:
            return None
        with self._transaction() as tx:
            task = tx.claim_task(supervisor, states)
            if task is None:
                return None
            task.lease_until = tx.now + self.lease
            tx.append(
                task.token, "task_claimed",
                task=task.id, supervisor=supervisor, attempt=task.attempt,
            )  # fmt: skip
            return self._grant(tx, task)

    def report(self, task: int, outcome: Outcome, *, supervisor: str | None = None) -> Fired:
        """The outcome of a task. With `supervisor`: from the supervisor that claimed
        it. Without: from outside (CI, a person), for a claimed or waiting task, with
        an event or a failure."""
        if supervisor is not None:
            _check_supervisor(supervisor)
        _check_outcome(outcome)
        with self._transaction() as tx:
            held = self._worked_task(tx, task, supervisor, waiting=outcome.kind == "waiting")
            row = self._existing(tx, held.token)
            result = self._report(tx, row, held, outcome)
        return _settled(result)

    def heartbeat(self, supervisor: str, tasks: Collection[int]) -> list[int]:
        """Renew the leases of the tasks that `supervisor` still holds. Returns the
        others, sorted: the token left the state, was cancelled, or the lease ended.
        The supervisor stops the routines of those tasks."""
        _check_supervisor(supervisor)
        with self._transaction() as tx:
            renewed = tx.renew_leases(supervisor, tasks, tx.now + self.lease)
        return sorted(set(tasks) - renewed)

    def note(self, task: int, text: str) -> None:
        """One line of progress in the token's history."""
        line = text.strip()
        if not 0 < len(line) <= NOTE_LENGTH:
            raise InvalidPayload(f"a note is 1 to {NOTE_LENGTH} characters")
        with self._transaction() as tx:
            held = self._worked_task(tx, task, None, waiting=False)
            tx.append(held.token, "note", task=held.id, text=line)

    def set_memory(self, task: int, key: str, value: Any) -> None:
        """Save one key of the routine memory of the task's token and state, at once.
        `None` deletes the key. The history records the key, never the value."""
        if not 0 < len(key) <= MEMORY_KEY_LENGTH:
            raise InvalidPayload(f"a memory key is 1 to {MEMORY_KEY_LENGTH} characters")
        if value is not None:
            try:
                json.dumps(value, allow_nan=False)
            except (TypeError, ValueError) as error:
                raise InvalidPayload(f"the value of {key!r} is not JSON: {error}") from None
        with self._transaction() as tx:
            held = self._worked_task(tx, task, None, waiting=False)
            if value is None:
                if tx.forget(held.token, held.state, key):
                    tx.append(held.token, "memory_deleted", state=held.state, memory_key=key)
            else:
                tx.set_memory(held.token, held.state, key, value)
                tx.append(held.token, "memory_set", state=held.state, memory_key=key)

    def _worked_task(
        self, tx: Transaction, task_id: int, supervisor: str | None, *, waiting: bool
    ) -> tables.Task:
        """The task, if it is still worked on, and, with `supervisor`, by it."""
        task = tx.task(task_id)
        if task is None:
            raise TaskNotFound(f"there is no task {task_id}")
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
        context = {**row.context, **outcome.context}
        if errors := self.graph.context_errors(context):
            raise InvalidContext("the context changes break the schema: " + "; ".join(errors))
        for schedule in outcome.schedules:
            if problem := self._schedule_problem(schedule):
                raise InvalidPayload(problem)
        if outcome.kind == "waiting":
            self._apply(tx, row, outcome, context)
            task.status, task.lease_until = "waiting", None
            tx.append(row.token, "task_waiting", task=task.id)
            return Fired(_token(row))
        event = outcome.event or ""
        emits = self.graph.state(task.state).emits
        if event not in emits:
            allowed = ", ".join(emits) or "nothing"
            reason = f"{task.state!r} may not emit {event!r}; it may emit: {allowed}"
            return self._reject(tx, row, event, reason, EventNotAllowed)
        if errors := self.graph.payload_errors(event, outcome.payload):
            raise InvalidPayload(f"{event}: " + "; ".join(errors))
        # The guards read the context with the routine's changes. Decide first: a
        # refused outcome saves none of the changes. A paused token decides too, so
        # it refuses what an active token refuses, and holds only what can apply.
        decision = self._decide(tx, row, event, outcome.payload, context)
        if isinstance(decision, _Refusal) and not decision.fail:
            return self._reject(tx, row, event, decision.reason, EventNotAllowed)
        self._apply(tx, row, outcome, context)
        # No idempotency key here: that key space is for a caller's `fire`. A task
        # is reported once, since `_worked_task` refuses a second report on it.
        refusal = self._route(tx, row, event, outcome.payload, key=None, task=task)
        if refusal is not None:
            raise RuntimeError(f"the decision found a transition, but routing refused: {refusal}")
        if row.status == "paused":  # the event is held; the work is done
            task.status, task.lease_until = "done", None
            tx.append(row.token, "task_done", task=task.id)
        return Fired(_token(row))

    def _apply(
        self, tx: Transaction, row: tables.Token, outcome: Outcome, context: dict[str, Any]
    ) -> None:
        """Save a report's context changes, and set its timers. The timers are set
        before the outcome moves the token, so that leaving the state cancels them."""
        if outcome.context:
            row.context = context
            tx.append(row.token, "context_changed", keys=sorted(outcome.context))
        for schedule in outcome.schedules:
            after = parse_duration(schedule.after)
            self._set_timer(tx, row, schedule.event, schedule.payload, after, kind="timer")

    def _schedule_problem(self, schedule: Schedule) -> str | None:
        """Why a routine's timer cannot be set, or None."""
        if problem := duration_problem(schedule.after):
            return f"a timer for {schedule.event!r}: {problem}"
        if schedule.event in OWN_EVENTS or not self.graph.declares(schedule.event):
            name = self.graph.name
            return f"a timer names {schedule.event!r}, which is not an event of {name!r}"
        if errors := self.graph.payload_errors(schedule.event, schedule.payload):
            return f"a timer for {schedule.event!r}: " + "; ".join(errors)
        return None

    def _task_failed(
        self, tx: Transaction, row: tables.Token, task: tables.Task, error: str
    ) -> None:
        """One failed attempt. Queue the task again while the state's retries allow;
        then the token gets `error`. With no way to go on `error`, the token fails."""
        tx.append(row.token, "task_failed", task=task.id, error=error)
        task.lease_until = None
        if task.attempt <= self.graph.state(task.state).retries:
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
        elif not self.graph.candidates(row.state, ERROR):
            self._fail(tx, row, f"{task.state!r} failed, and has no transition on 'error': {error}")
        elif (refusal := self._route(tx, row, ERROR, None, key=None)) is not None:
            self._fail(tx, row, f"{task.state!r} failed, and 'error' cannot move it: {refusal}")

    def _grant(self, tx: Transaction, task: tables.Task) -> TaskGrant:
        row = self._existing(tx, task.token)
        last = tx.last_accepted(task.token)
        entered_by = (
            None if last is None else EventRecord(last.data["event"], last.data.get("payload"))
        )
        return TaskGrant(
            task=task.id,
            token=task.token,
            state=task.state,
            visit=task.visit,
            attempt=task.attempt,
            context=dict(row.context),
            memory=tx.memory(task.token, task.state),
            entered_by=entered_by,
        )

    # ── time ────────────────────────────────────────────────────────────

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

    def _create(self, tx: Transaction, token: str, context: dict[str, Any]) -> tables.Token:
        values = self.graph.with_defaults(context)
        if problem := _not_json(values):
            raise InvalidContext(f"the context is not JSON: {problem}")
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


def _held_events(entries: list[tables.LedgerEntry]) -> list[_Held]:
    """The events a paused token holds: those deferred since it was last paused.
    Each resume settles all of them, so nothing older is still held."""
    since = max((e.seq for e in entries if e.kind == "paused"), default=0)
    return [
        _Held(
            event=e.data["event"],
            payload=e.data.get("payload"),
            origin=(e.data["state"], e.data["visit"]) if "state" in e.data else None,
        )
        for e in entries
        if e.kind == "event_deferred" and e.seq > since
    ]
