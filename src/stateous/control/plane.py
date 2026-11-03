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
import re
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, cast

from sqlalchemy.engine import Engine as Database

from stateous.control.access import Action, Authorize
from stateous.control.clock import LOST, TICK_EVERY, Clock
from stateous.control.engine import (
    DEFAULT_LEASE,
    LABEL_KEY,
    Engine,
    Fired,
    HistoryEntry,
    RecordedGraph,
    Token,
    check_limit,
    check_start_key,
    utcnow,
)
from stateous.control.errors import (
    Forbidden,
    GraphConflict,
    HoldLost,
    InvalidArgument,
    NoticesOff,
    ScopeConflict,
    TaskNotFound,
    TokenNotFound,
)
from stateous.control.graph import Graph
from stateous.control.ids import new_id
from stateous.control.notices import Notice, deliver
from stateous.control.scope import (
    MAX_SCOPE_KEYS,
    AllScopes,
    digest_of,
    key_of,
    scope_digest,
)
from stateous.control.store import Store
from stateous.model.graph_file import load_graph
from stateous.model.wire import Outcome, TaskGrant

log = logging.getLogger("stateous.notices")

Scope = Mapping[str, str] | AllScopes | None
"""The scope of a call: its values, `ALL_SCOPES`, or None (then `scope_of(caller)`)."""

ScopeOf = Callable[[object], Mapping[str, str] | AllScopes | None]
"""The developer's function: the scope of a caller."""

_RAISE = object()

LIST_PAGE = 1000
"""Tokens that a list with an authorizer reads at a time, until its limit is full."""

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
    With no authorizer, every call is allowed.

    `exclusive=False` (Postgres only) makes this control plane one of several
    replicas of the namespace: it takes no hold, and its clock runs beside theirs.
    `direct` is a direct connection for session-level work (the hold, LISTEN), for a
    `database` that goes through a pooler in transaction mode."""

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
        exclusive: bool = True,
        direct: str | Database | None = None,
        scope: Sequence[str] = (),
        scope_of: ScopeOf | None = None,
    ) -> None:
        self._authorize = authorize
        self._scope_keys = _scope_keys(scope)
        self._scope_of = scope_of
        self._exclusive = exclusive
        self._graph = Graph(_document(graph))
        self.name: str = self._graph.name
        """The workflow's name."""
        self._store = Store(
            database,
            namespace=namespace,
            hold=exclusive,
            migrate=migrate,
            replica=not exclusive,
            direct=direct,
        )
        try:
            self._engine = Engine(self._store, self._graph, clock=now, lease=lease)
            self._check_scope()
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
        token: str | None,
        context: dict[str, Any],
        *,
        labels: Mapping[str, str] | None = None,
        key: str | None = None,
        scope: Scope = None,
        caller: object = None,
    ) -> Token:
        """Start a token. To start a token that exists returns it, unchanged. `labels`
        (at most 16) never change after the start; a child token gets them too. In a
        scoped namespace the labels give the token's scope; a call scope that differs
        is `Forbidden`. The authorizer checks `start` with `labels`, and `read` for a
        token that exists with other labels.

        With `token` None, Stateous makes an id: a ULID, so ids made later sort later.
        With `key` too, a retry with the same key in the same scope gets the same
        token. A start that names its id gives no `key`."""
        if token is not None and key is not None:
            raise InvalidArgument("give a token id or an idempotency key, not both")
        if key is not None:
            check_start_key(key)
        made = token if token is not None else new_id()
        found = self._start_key(made, labels or {}, scope, caller)
        self._allow(caller, "start", labels or {})
        start_key = None if key is None else (digest_of(found) or "", key)
        started = self._engine.start(found, context, labels=labels, start_key=start_key)
        if started.labels != dict(labels or {}):
            self._allow(caller, "read", started.labels)
        return started

    def known_start_key(
        self,
        key: str,
        *,
        labels: Mapping[str, str] | None = None,
        scope: Scope = None,
        caller: object = None,
    ) -> bool:
        """Whether a start with this idempotency key made a token in the scope that
        `labels` give (or the call's scope)."""
        check_start_key(key)
        found = self._start_key("x", labels or {}, scope, caller) if self._scope_keys else "x"
        return self._engine.known_start_key(digest_of(found) or "", key)

    def exists_for_start(
        self,
        token: str,
        *,
        labels: Mapping[str, str] | None = None,
        scope: Scope = None,
        caller: object = None,
    ) -> bool:
        """Whether a start of `token` with `labels` finds a token that exists: in the
        scope that the labels give."""
        try:
            self._engine.get(self._start_key(token, labels or {}, scope, caller))
        except TokenNotFound:
            return False
        return True

    def get(self, token: str, *, scope: Scope = None, caller: object = None) -> Token:
        """The token. Raises `TokenNotFound`, also for a token of another scope."""
        found = self._engine.get(self._key(token, scope, caller))
        self._allow(caller, "read", found.labels)
        return found

    def tokens(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        parent: str | None = None,
        labels: Mapping[str, str] | None = None,
        scope: Scope = None,
        caller: object = None,
    ) -> list[Token]:
        """Tokens of the call's scope, the most recently moved first. With `parent`:
        only its children. With `labels`: only the tokens that have each of them. With
        an authorizer: only the tokens that the caller may read, up to `limit` of
        them."""
        in_scope = self._scope(scope, caller)
        wanted = self._with_scope(labels, in_scope)
        if wanted is None:
            return []
        parent_key = None if parent is None else self._key(parent, scope, caller, missing=None)
        if parent is not None and parent_key is None:
            return []
        if self._authorize is None:
            return self._engine.tokens(status=status, limit=limit, parent=parent_key, labels=wanted)
        check_limit(limit)
        allowed: list[Token] = []
        after: Token | None = None
        while len(allowed) < limit:
            page = self._engine.tokens(
                status=status, limit=LIST_PAGE, parent=parent_key, labels=wanted, after=after
            )
            allowed.extend(t for t in page if self._authorize(caller, "read", t.labels))
            if len(page) < LIST_PAGE:
                break
            after = page[-1]
        return allowed[:limit]

    def history(
        self, token: str, *, scope: Scope = None, caller: object = None
    ) -> list[HistoryEntry]:
        """The ledger of the token, oldest first."""
        key = self._key(token, scope, caller)
        self._allow(caller, "read", self._engine.get(key).labels)
        return self._engine.history(key)

    def fire(
        self,
        token: str,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        key: str | None = None,
        scope: Scope = None,
        caller: object = None,
    ) -> Fired:
        """Fire `event` on `token` from outside. With `key`: a second call with the
        same key changes nothing and answers `duplicate`."""
        found = self._key(token, scope, caller)
        self._allow_token(caller, "fire", found)
        return self._engine.fire(found, event, payload, key=key)

    def pause(self, token: str, *, scope: Scope = None, caller: object = None) -> Token:
        """Stop the token: its events are held, its tasks wait, its timers stand still."""
        found = self._key(token, scope, caller)
        self._allow_token(caller, "pause", found)
        return self._engine.pause(found)

    def resume(self, token: str, *, scope: Scope = None, caller: object = None) -> Token:
        """Start a paused token again. Its held events apply in order."""
        found = self._key(token, scope, caller)
        self._allow_token(caller, "resume", found)
        return self._engine.resume(found)

    def cancel(self, token: str, *, scope: Scope = None, caller: object = None) -> Token:
        """End the token as cancelled, with its children."""
        found = self._key(token, scope, caller)
        self._allow_token(caller, "cancel", found)
        return self._engine.cancel(found)

    # ── supervisors ─────────────────────────────────────────────────────

    def claim(
        self,
        supervisor: str,
        states: Collection[str],
        *,
        request: str | None = None,
        labels: Mapping[str, str] | None = None,
        scope: Scope = None,
        caller: object = None,
    ) -> TaskGrant | None:
        """The oldest queued task of `states` in the call's scope, now claimed by
        `supervisor` with a lease; None if there is none. With `labels`: only a task
        whose token has each of them; the authorizer checks `claim` with these labels.
        A repeat with the same `request` gets the same grant again."""
        wanted = self._with_scope(labels, self._scope(scope, caller))
        if wanted is None:
            return None
        self._allow(caller, "claim", wanted)
        return self._engine.claim(supervisor, states, request=request, labels=wanted or None)

    def count_claimed(self, labels: Mapping[str, str]) -> int:
        """How many tasks are claimed now whose token has each of `labels`: for an
        entitlement, for example "about 3 tasks at a time on this plan"."""
        return self._engine.count_claimed(labels)

    def report(
        self,
        task: int,
        outcome: Outcome,
        *,
        supervisor: str | None = None,
        attempt: int | None = None,
        scope: Scope = None,
        caller: object = None,
    ) -> Fired:
        """The outcome of a task: from its supervisor, or (without `supervisor`) from
        outside. A task of another scope is `TaskNotFound`."""
        self._task_in_scope(task, scope, caller)
        self._allow_task(caller, "report", task)
        return self._engine.report(task, outcome, supervisor=supervisor, attempt=attempt)

    def heartbeat(
        self,
        supervisor: str,
        tasks: Collection[int],
        *,
        scope: Scope = None,
        caller: object = None,
    ) -> list[int]:
        """Renew the leases of `tasks`. Returns the tasks that the supervisor lost. A
        task of another scope, or one that the authorizer refuses, is lost too, and
        its lease is not renewed."""
        outside = self._outside_scope(tasks, scope, caller)
        tasks = [t for t in tasks if t not in outside]
        if self._authorize is None:
            lost = set(self._engine.heartbeat(supervisor, tasks)) if tasks else set[int]()
            return sorted(lost | outside)
        labels = self._engine.task_labels(tasks)
        check = self._authorize
        refused = {t for t, found in labels.items() if not check(caller, "heartbeat", found)}
        allowed = [t for t in tasks if t not in refused]
        lost = set(self._engine.heartbeat(supervisor, allowed)) if allowed else set[int]()
        return sorted(lost | refused | outside)

    def note(
        self,
        task: int,
        text: str,
        *,
        attempt: int | None = None,
        scope: Scope = None,
        caller: object = None,
    ) -> None:
        """One line of progress in the token's history."""
        self._task_in_scope(task, scope, caller)
        self._allow_task(caller, "note", task)
        self._engine.note(task, text, attempt=attempt)

    def set_memory(
        self,
        task: int,
        key: str,
        value: Any,
        *,
        attempt: int | None = None,
        scope: Scope = None,
        caller: object = None,
    ) -> None:
        """Save one key of the routine's memory for the task's token and state."""
        self._task_in_scope(task, scope, caller)
        self._allow_task(caller, "memory", task)
        self._engine.set_memory(task, key, value, attempt=attempt)

    # ── scopes ──────────────────────────────────────────────────────────

    def _scope(self, scope: Scope, caller: object) -> dict[str, str] | AllScopes | None:
        """The call's scope: `scope=`, else `scope_of(caller)`. None in a namespace with
        no scope. In a scoped one, no scope at all is `InvalidArgument`."""
        if not self._scope_keys:
            return None
        found = (
            scope
            if scope is not None
            else (self._scope_of(caller) if self._scope_of is not None else None)
        )
        if found is None:
            raise InvalidArgument("this control plane keeps token ids for each scope; give scope=")
        if isinstance(found, AllScopes):
            return found
        missing = [k for k in self._scope_keys if k not in found]
        if missing:
            raise InvalidArgument(f"the scope has no value for {', '.join(missing)}")
        return {k: str(found[k]) for k in self._scope_keys}

    def _key(self, token: str, scope: Scope, caller: object, *, missing: object = _RAISE) -> str:
        """The key of `token` in the call's scope. With `ALL_SCOPES`: the one key whose
        public id is `token`. Not there: `TokenNotFound`, or `missing` when given."""
        in_scope = self._scope(scope, caller)
        if in_scope is None:
            return token
        if not isinstance(in_scope, AllScopes):
            return key_of(scope_digest(in_scope), token)
        keys = self._engine.keys_for_public(token)
        if len(keys) == 1:
            return keys[0]
        if len(keys) > 1:
            raise InvalidArgument(f"the id {token!r} is in {len(keys)} scopes; give scope=")
        if missing is not _RAISE:
            return cast("str", missing)
        raise TokenNotFound(f"there is no token {token!r}")

    def _start_key(
        self, token: str, labels: Mapping[str, str], scope: Scope, caller: object
    ) -> str:
        """The key of a new token: its scope comes from its labels. A call scope that
        names another scope is `Forbidden`."""
        if not self._scope_keys:
            return token
        missing = [k for k in self._scope_keys if k not in labels]
        if missing:
            raise InvalidArgument(
                f"a token of this namespace needs the labels {', '.join(missing)}: its scope"
            )
        own = {k: str(labels[k]) for k in self._scope_keys}
        asked = self._scope(scope, caller)  # no scope at all is InvalidArgument here too
        if not isinstance(asked, AllScopes) and asked != own:
            raise Forbidden("the token's labels name another scope than the caller's")
        return key_of(scope_digest(own), token)

    def _with_scope(
        self, labels: Mapping[str, str] | None, in_scope: dict[str, str] | AllScopes | None
    ) -> dict[str, str] | None:
        """`labels`, with the scope's pairs added. None when they name another scope:
        nothing can match."""
        wanted = dict(labels or {})
        if in_scope is None or isinstance(in_scope, AllScopes):
            return wanted
        for k, v in in_scope.items():
            if wanted.setdefault(k, v) != v:
                return None
        return wanted

    def _outside_scope(self, tasks: Collection[int], scope: Scope, caller: object) -> set[int]:
        """The tasks of `tasks` whose token is in another scope than the call's."""
        in_scope = self._scope(scope, caller)
        if in_scope is None or isinstance(in_scope, AllScopes):
            return set()
        digest = scope_digest(in_scope)
        keys = self._engine.task_keys(tasks)
        return {t for t, k in keys.items() if digest_of(k) != digest}

    def _task_in_scope(self, task: int, scope: Scope, caller: object) -> None:
        if self._outside_scope([task], scope, caller):
            raise TaskNotFound(f"there is no task {task}")

    def _check_scope(self) -> None:
        """The namespace's scope must be this plane's, or the namespace has no token."""
        mine = list(self._scope_keys)
        recorded = self._store.recorded_scope()
        if recorded is not None and recorded != mine and self._store.count_tokens() > 0:
            raise ScopeConflict(
                f"the namespace keeps its tokens under the scope {recorded}, not {mine}; "
                "a new scope needs a namespace with no token"
            )
        if self._store.is_postgres and any(
            other != json.dumps(mine) for other in self._store.live_replica_scopes()
        ):
            raise ScopeConflict("a replica of this namespace serves another scope")
        if recorded != mine:
            self._store.record_scope(mine)

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
        """Refuse a different structure while tokens of the one on record are active or
        paused, or while a replica serves another structure. The same structure with
        another checksum (only timeouts and retries differ) opens, with a warning. Then
        put this graph on record, if it is not there yet; a replica writes its row."""
        graph = self._graph
        recorded = self._engine.recorded_graph()
        others = self._store.live_replicas() if self._store.is_postgres else []
        if recorded is not None and recorded.structure != graph.structure:
            live = self._engine.live_tokens()
            if live > 0:
                raise GraphConflict(
                    f"refusing to serve: {live} token(s) are active or paused on another "
                    f"structure of the workflow {graph.name!r}; serve the graph file they "
                    "started on until they end, or cancel them"
                )
        if conflicting := [
            holder for holder, structure, _ in others if structure != graph.structure
        ]:
            raise GraphConflict(
                f"refusing to serve: {len(conflicting)} replica(s) serve another structure of "
                f"the workflow {graph.name!r}; stop them first, or use another namespace"
            )
        same_structure = recorded is not None and recorded.structure == graph.structure
        if same_structure and recorded is not None and recorded.checksum != graph.checksum:
            log.warning(
                "the graph of %r has another checksum than the one on record (only "
                "timeouts or retries differ); the newest one is put on record",
                graph.name,
            )
        if recorded != RecordedGraph(graph.structure, graph.checksum):
            self._engine.record_graph()
        if not self._exclusive:
            self._store.join_replicas(
                graph.structure, graph.checksum, json.dumps(list(self._scope_keys))
            )


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
        self,
        token: str | None,
        context: dict[str, Any],
        *,
        labels: Mapping[str, str] | None = None,
        key: str | None = None,
    ) -> Token:
        return self.plane.start(token, context, labels=labels, key=key, caller=self.caller)

    def known_start_key(self, key: str, *, labels: Mapping[str, str] | None = None) -> bool:
        return self.plane.known_start_key(key, labels=labels, caller=self.caller)

    def exists_for_start(self, token: str, *, labels: Mapping[str, str] | None = None) -> bool:
        return self.plane.exists_for_start(token, labels=labels, caller=self.caller)

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


def _scope_keys(scope: Sequence[str]) -> tuple[str, ...]:
    """The label keys of a scope: at most `MAX_SCOPE_KEYS`, each a valid label key."""
    keys = tuple(scope)
    if len(keys) > MAX_SCOPE_KEYS:
        raise ValueError(f"a scope has at most {MAX_SCOPE_KEYS} label keys, not {len(keys)}")
    for key in keys:
        if not re.fullmatch(LABEL_KEY, key):
            raise ValueError(f"the scope key {key!r} is not a label key")
    return keys
