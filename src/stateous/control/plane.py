"""`ControlPlane`: one compiled workflow over one database, as a library object.

A developer makes it in their own service, with a database URL or their own Postgres
engine. It holds the database (one serving control plane for each namespace), checks
the graph against the one on record, and gives the calls of tokens and of
supervisors. `stateous serve` is a small program on top of it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

from sqlalchemy.engine import Engine as Database

from stateous.control.access import Action, Authorize
from stateous.control.clock import LOST, TICK_EVERY, Clock
from stateous.control.engine import (
    DEFAULT_LEASE,
    MOST_TOKENS,
    Engine,
    Fired,
    HistoryEntry,
    RecordedGraph,
    Token,
    utcnow,
)
from stateous.control.errors import Forbidden, GraphConflict, HoldLost, NoticesOff
from stateous.control.graph import Graph
from stateous.control.notices import Notice, deliver
from stateous.control.store import Store
from stateous.model.graph_file import load_graph
from stateous.model.wire import Outcome, TaskGrant

log = logging.getLogger("stateous.notices")

NOTICES_EVERY = 1.0
"""Seconds between two rounds of `run_notices` when no notice waits."""


class ControlPlane:
    """One workflow over one database: its tokens, its tasks and its clock.

    `graph` is a graph file's path, or a graph document (the parsed JSON).
    `database` is a URL, or a Postgres engine that the developer gives. On Postgres,
    `namespace` is the schema of the tables (default `stateous`). With `migrate=True`
    the tables are made or updated at once; else tables that are missing or out of
    date raise `SchemaNotReady`, which names `stateous migrate`. `now` is the time of
    each transaction: tests give a clock that they move by hand.

    It holds its namespace until `close()`: a second control plane on the same
    namespace raises `DatabaseHeld`. A different structure of the workflow on record,
    with tokens still active or paused, raises `GraphConflict`.

    `authorize(caller, action, labels)` decides each call; a refusal raises
    `Forbidden`. Each call takes the keyword `caller`, which the transport gives.
    With no authorizer, every call is allowed."""

    def __init__(
        self,
        graph: str | Path | dict[str, Any],
        *,
        database: str | Database,
        namespace: str | None = None,
        migrate: bool = False,
        lease: timedelta = DEFAULT_LEASE,
        now: Callable[[], datetime] = utcnow,
        authorize: Authorize | None = None,
    ) -> None:
        self._authorize = authorize
        self._graph = Graph(_document(graph))
        self.name: str = self._graph.name
        """The workflow's name."""
        self._store = Store(database, namespace=namespace, hold=True, migrate=migrate)
        try:
            self._engine = Engine(self._store, self._graph, clock=now, lease=lease)
            self._check_graph()
        except BaseException:
            self._store.close()
            raise
        self.clock = Clock(self._engine, self._store)
        """The clock that `run_clock` runs. `stateous serve` runs it in its app."""
        self._closed = threading.Event()
        self._lost = False

    @property
    def lost(self) -> bool:
        """True after another control plane took the database: this one must stop."""
        return self._lost or self.clock.lost

    # ── the workflow ────────────────────────────────────────────────────

    @property
    def document(self) -> dict[str, Any]:
        """The graph document: what `GET /workflow` answers."""
        return self._graph.document

    def health(self) -> dict[str, Any]:
        """What `GET /health` answers: the workflow's name and its checksum."""
        return {"workflow": self._graph.name, "checksum": self._graph.checksum}

    def on_task_ready(
        self, callback: Callable[[str, Mapping[str, str]], None]
    ) -> Callable[[], None]:
        """Call `callback(state, labels)` after each commit that makes a task of
        `state` claimable: a new task, a retry, or a resume. A child's state is
        `<workflow>/<state>`, as a claim names it; `labels` are its token's. The
        callback runs on the thread of the change; it must be quick. A callback that
        raises is logged, and the change stays. Returns the function that removes the
        callback.

        A transport uses it to wake a claim that waits, or to publish "work for these
        states" to a queue. It is a hint: the claim is still the only way to a task."""
        return self._engine.on_task_ready(callback)

    # ── tokens ──────────────────────────────────────────────────────────

    def start(
        self,
        token: str,
        context: dict[str, Any],
        *,
        labels: Mapping[str, str] | None = None,
        caller: object = None,
    ) -> Token:
        """Start a token. To start a token that exists returns it, unchanged. `labels`
        (at most 16) never change after the start; a child token gets them too. The
        authorizer checks `start` with `labels`, and `read` for a token that exists
        with other labels."""
        self._allow(caller, "start", labels or {})
        started = self._engine.start(token, context, labels=labels)
        if started.labels != dict(labels or {}):
            self._allow(caller, "read", started.labels)
        return started

    def get(self, token: str, *, caller: object = None) -> Token:
        """The token. Raises `TokenNotFound`."""
        found = self._engine.get(token)
        self._allow(caller, "read", found.labels)
        return found

    def tokens(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        parent: str | None = None,
        labels: Mapping[str, str] | None = None,
        caller: object = None,
    ) -> list[Token]:
        """Tokens, the most recently moved first. With `parent`: only its children.
        With `labels`: only the tokens that have each of them. With an authorizer:
        only the tokens that the caller may read, up to `limit` of them."""
        if self._authorize is None:
            return self._engine.tokens(status=status, limit=limit, parent=parent, labels=labels)
        found = self._engine.tokens(status=status, limit=MOST_TOKENS, parent=parent, labels=labels)
        allowed = [t for t in found if self._authorize(caller, "read", t.labels)]
        return allowed[:limit]

    def history(self, token: str, *, caller: object = None) -> list[HistoryEntry]:
        """The ledger of the token, oldest first."""
        self.get(token, caller=caller)
        return self._engine.history(token)

    def fire(
        self,
        token: str,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        key: str | None = None,
        caller: object = None,
    ) -> Fired:
        """Fire `event` on `token` from outside. With `key`: a second call with the
        same key changes nothing and answers `duplicate`."""
        self._allow_token(caller, "fire", token)
        return self._engine.fire(token, event, payload, key=key)

    def pause(self, token: str, *, caller: object = None) -> Token:
        """Stop the token: its events are held, its tasks wait, its timers stand still."""
        self._allow_token(caller, "pause", token)
        return self._engine.pause(token)

    def resume(self, token: str, *, caller: object = None) -> Token:
        """Start a paused token again. Its held events apply in order."""
        self._allow_token(caller, "resume", token)
        return self._engine.resume(token)

    def cancel(self, token: str, *, caller: object = None) -> Token:
        """End the token as cancelled, with its children."""
        self._allow_token(caller, "cancel", token)
        return self._engine.cancel(token)

    # ── supervisors ─────────────────────────────────────────────────────

    def claim(
        self,
        supervisor: str,
        states: Collection[str],
        *,
        request: str | None = None,
        labels: Mapping[str, str] | None = None,
        caller: object = None,
    ) -> TaskGrant | None:
        """The oldest queued task of `states`, now claimed by `supervisor` with a
        lease; None if there is none. With `labels`: only a task whose token has each
        of them; the authorizer checks `claim` with these labels. A repeat with the
        same `request` gets the same grant again."""
        self._allow(caller, "claim", labels or {})
        return self._engine.claim(supervisor, states, request=request, labels=labels)

    def count_claimed(self, labels: Mapping[str, str]) -> int:
        """How many tasks are claimed now whose token has each of `labels`: for an
        entitlement, for example "3 tasks at a time on this plan"."""
        return self._engine.count_claimed(labels)

    def report(
        self,
        task: int,
        outcome: Outcome,
        *,
        supervisor: str | None = None,
        attempt: int | None = None,
        caller: object = None,
    ) -> Fired:
        """The outcome of a task: from its supervisor, or (without `supervisor`) from
        outside."""
        self._allow_task(caller, "report", task)
        return self._engine.report(task, outcome, supervisor=supervisor, attempt=attempt)

    def heartbeat(
        self, supervisor: str, tasks: Collection[int], *, caller: object = None
    ) -> list[int]:
        """Renew the leases of `tasks`. Returns the tasks that the supervisor lost. A
        task that the authorizer refuses is lost too, and its lease is not renewed."""
        if self._authorize is None:
            return self._engine.heartbeat(supervisor, tasks)
        labels = self._engine.task_labels(tasks)
        check = self._authorize
        refused = {t for t, found in labels.items() if not check(caller, "heartbeat", found)}
        allowed = [t for t in tasks if t not in refused]
        lost = set(self._engine.heartbeat(supervisor, allowed)) if allowed else set[int]()
        return sorted(lost | refused)

    def note(
        self, task: int, text: str, *, attempt: int | None = None, caller: object = None
    ) -> None:
        """One line of progress in the token's history."""
        self._allow_task(caller, "note", task)
        self._engine.note(task, text, attempt=attempt)

    def set_memory(
        self,
        task: int,
        key: str,
        value: Any,
        *,
        attempt: int | None = None,
        caller: object = None,
    ) -> None:
        """Save one key of the routine's memory for the task's token and state."""
        self._allow_task(caller, "memory", task)
        self._engine.set_memory(task, key, value, attempt=attempt)

    def as_caller(self, caller: object) -> AsCaller:
        """This control plane, with `caller` given to each call: for a transport that
        knows its caller once, for example the HTTP API for one request."""
        return AsCaller(self, caller)

    # ── who may do what ─────────────────────────────────────────────────

    def _allow(self, caller: object, action: Action, labels: Mapping[str, str]) -> None:
        if self._authorize is not None and not self._authorize(caller, action, labels):
            raise Forbidden(f"the caller may not {action} with the labels {dict(labels)}")

    def _allow_token(self, caller: object, action: Action, token: str) -> None:
        if self._authorize is not None:
            self._allow(caller, action, self._engine.get(token).labels)

    def _allow_task(self, caller: object, action: Action, task: int) -> None:
        """An unknown task is left to the engine: it raises `TaskNotFound`."""
        if self._authorize is not None:
            found = self._engine.task_labels([task])
            if task in found:
                self._allow(caller, action, found[task])

    # ── the clock ───────────────────────────────────────────────────────

    def tick(self) -> int:
        """One step of the clock: renew the hold, then end expired leases and fire due
        timers. Returns how many. Raises `HoldLost` when another control plane took
        the database."""
        if not self._store.keep_serving():
            self._lost = True
            raise HoldLost(LOST)
        return self._engine.tick()

    async def run_clock(self, *, every: float = TICK_EVERY) -> None:
        """Run the clock in this event loop, until `close()`. Raises `HoldLost` when
        another control plane took the database. A step that fails is logged, and
        the clock goes on."""
        self.clock.every = every
        await self.clock.run()
        if self.clock.lost:
            raise HoldLost(LOST)

    # ── the tables ──────────────────────────────────────────────────────

    def migrate(self, *, notices: bool | None = None) -> None:
        """Bring the tables to this version's schema. `notices` turns the notices of the
        database on or off; None keeps the setting. Other control planes on the same
        database read the setting when they open."""
        self._store.migrate(notices=notices)

    # ── notices ─────────────────────────────────────────────────────────

    def deliver_notices(self, handler: Callable[[Notice], None], *, limit: int = 100) -> int:
        """Hand the waiting notices to `handler`, and delete each one that it has.
        Returns how many. The notices of one token family arrive in order. A handler
        that raises gets the same notices again on the next call. Keep the handler
        short: hand slow work to your own queue. Raises `NoticesOff` when the
        database has notices off."""
        if not self._store.notices_on:
            raise NoticesOff(
                "notices are off for this database; run: stateous migrate --db URL --notices"
            )
        return deliver(self._store, self.name, handler, limit=limit)

    async def run_notices(
        self, handler: Callable[[Notice], None], *, every: float = NOTICES_EVERY
    ) -> None:
        """Deliver the notices in this event loop, until `close()`. Each round runs on
        a worker thread; when no notice waits, the next round is `every` seconds
        later. A round that fails (the database does not answer) is logged, and the
        loop goes on."""
        while not self._closed.is_set():
            try:
                delivered = await asyncio.to_thread(self.deliver_notices, handler)
            except NoticesOff:
                raise
            except Exception as error:  # e.g. the database stopped answering: one line
                log.error(
                    "the notices could not be delivered (%s); trying again in %s seconds",
                    " ".join(str(error).split()) or type(error).__name__,
                    every,
                )
                delivered = 0
            if delivered == 0 and not self._closed.is_set():
                await asyncio.sleep(every)

    def close(self) -> None:
        """Stop the clock and the notices, give up the hold and let go of the
        connections."""
        self._closed.set()
        self.clock.stop()
        self._store.close()

    def __enter__(self) -> ControlPlane:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.close()

    # ── the graph on record ─────────────────────────────────────────────

    def _check_graph(self) -> None:
        """A different structure on record, while tokens are active or paused: refuse.
        Else put this graph on record, if it is not there yet."""
        graph = self._graph
        recorded = self._engine.recorded_graph()
        if recorded is not None and recorded.structure != graph.structure:
            live = self._engine.live_tokens()
            if live > 0:
                raise GraphConflict(
                    f"refusing to serve: {live} token(s) are active or paused on another "
                    f"structure of the workflow {graph.name!r}; serve the graph file they "
                    "started on until they end, or cancel them"
                )
        if recorded != RecordedGraph(graph.structure, graph.checksum):
            self._engine.record_graph()


def _document(graph: str | Path | dict[str, Any]) -> dict[str, Any]:
    """The graph document, checked as a graph file is."""
    if isinstance(graph, dict):
        return load_graph(json.dumps(graph))
    return load_graph(Path(graph).read_text(encoding="utf-8"))


class AsCaller:
    """A control plane with one caller given to each call (`ControlPlane.as_caller`)."""

    def __init__(self, plane: ControlPlane, caller: object) -> None:
        self.plane = plane
        self.caller = caller

    @property
    def document(self) -> dict[str, Any]:
        return self.plane.document

    @property
    def lost(self) -> bool:
        return self.plane.lost

    def health(self) -> dict[str, Any]:
        return self.plane.health()

    def on_task_ready(
        self, callback: Callable[[str, Mapping[str, str]], None]
    ) -> Callable[[], None]:
        return self.plane.on_task_ready(callback)

    def start(
        self, token: str, context: dict[str, Any], *, labels: Mapping[str, str] | None = None
    ) -> Token:
        return self.plane.start(token, context, labels=labels, caller=self.caller)

    def get(self, token: str) -> Token:
        return self.plane.get(token, caller=self.caller)

    def tokens(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        parent: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> list[Token]:
        return self.plane.tokens(
            status=status, limit=limit, parent=parent, labels=labels, caller=self.caller
        )

    def history(self, token: str) -> list[HistoryEntry]:
        return self.plane.history(token, caller=self.caller)

    def fire(
        self, token: str, event: str, payload: dict[str, Any] | None, *, key: str | None = None
    ) -> Fired:
        return self.plane.fire(token, event, payload, key=key, caller=self.caller)

    def pause(self, token: str) -> Token:
        return self.plane.pause(token, caller=self.caller)

    def resume(self, token: str) -> Token:
        return self.plane.resume(token, caller=self.caller)

    def cancel(self, token: str) -> Token:
        return self.plane.cancel(token, caller=self.caller)

    def claim(
        self,
        supervisor: str,
        states: Collection[str],
        *,
        request: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> TaskGrant | None:
        return self.plane.claim(
            supervisor, states, request=request, labels=labels, caller=self.caller
        )

    def report(
        self,
        task: int,
        outcome: Outcome,
        *,
        supervisor: str | None = None,
        attempt: int | None = None,
    ) -> Fired:
        return self.plane.report(
            task, outcome, supervisor=supervisor, attempt=attempt, caller=self.caller
        )

    def heartbeat(self, supervisor: str, tasks: Collection[int]) -> list[int]:
        return self.plane.heartbeat(supervisor, tasks, caller=self.caller)

    def note(self, task: int, text: str, *, attempt: int | None = None) -> None:
        self.plane.note(task, text, attempt=attempt, caller=self.caller)

    def set_memory(self, task: int, key: str, value: Any, *, attempt: int | None = None) -> None:
        self.plane.set_memory(task, key, value, attempt=attempt, caller=self.caller)
