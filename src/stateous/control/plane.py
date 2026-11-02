"""`ControlPlane`: one compiled workflow over one database, as a library object.

A developer makes it in their own service, with a database URL or their own Postgres
engine. It holds the database (one serving control plane for each namespace), checks
the graph against the one on record, and gives the calls of tokens and of
supervisors. `stateous serve` is a small program on top of it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

from sqlalchemy.engine import Engine as Database

from stateous.control.clock import LOST, TICK_EVERY, Clock
from stateous.control.engine import (
    DEFAULT_LEASE,
    Engine,
    Fired,
    HistoryEntry,
    RecordedGraph,
    Token,
    utcnow,
)
from stateous.control.errors import GraphConflict, HoldLost
from stateous.control.graph import Graph
from stateous.control.store import Store
from stateous.model.graph_file import load_graph
from stateous.model.wire import Outcome, TaskGrant


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
    with tokens still active or paused, raises `GraphConflict`."""

    def __init__(
        self,
        graph: str | Path | dict[str, Any],
        *,
        database: str | Database,
        namespace: str | None = None,
        migrate: bool = False,
        lease: timedelta = DEFAULT_LEASE,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
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

    # ── the workflow ────────────────────────────────────────────────────

    @property
    def document(self) -> dict[str, Any]:
        """The graph document: what `GET /workflow` answers."""
        return self._graph.document

    def health(self) -> dict[str, Any]:
        """What `GET /health` answers: the workflow's name and its checksum."""
        return {"workflow": self._graph.name, "checksum": self._graph.checksum}

    # ── tokens ──────────────────────────────────────────────────────────

    def start(self, token: str, context: dict[str, Any]) -> Token:
        """Start a token. To start a token that exists returns it, unchanged."""
        return self._engine.start(token, context)

    def get(self, token: str) -> Token:
        """The token. Raises `TokenNotFound`."""
        return self._engine.get(token)

    def tokens(
        self, *, status: str | None = None, limit: int = 100, parent: str | None = None
    ) -> list[Token]:
        """Tokens, the most recently moved first. With `parent`: only its children."""
        return self._engine.tokens(status=status, limit=limit, parent=parent)

    def history(self, token: str) -> list[HistoryEntry]:
        """The ledger of the token, oldest first."""
        return self._engine.history(token)

    def fire(
        self,
        token: str,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        key: str | None = None,
    ) -> Fired:
        """Fire `event` on `token` from outside. With `key`: a second call with the
        same key changes nothing and answers `duplicate`."""
        return self._engine.fire(token, event, payload, key=key)

    def pause(self, token: str) -> Token:
        """Stop the token: its events are held, its tasks wait, its timers stand still."""
        return self._engine.pause(token)

    def resume(self, token: str) -> Token:
        """Start a paused token again. Its held events apply in order."""
        return self._engine.resume(token)

    def cancel(self, token: str) -> Token:
        """End the token as cancelled, with its children."""
        return self._engine.cancel(token)

    # ── supervisors ─────────────────────────────────────────────────────

    def claim(
        self, supervisor: str, states: Collection[str], *, request: str | None = None
    ) -> TaskGrant | None:
        """The oldest queued task of `states`, now claimed by `supervisor` with a
        lease; None if there is none. A repeat with the same `request` gets the same
        grant again."""
        return self._engine.claim(supervisor, states, request=request)

    def report(
        self,
        task: int,
        outcome: Outcome,
        *,
        supervisor: str | None = None,
        attempt: int | None = None,
    ) -> Fired:
        """The outcome of a task: from its supervisor, or (without `supervisor`) from
        outside."""
        return self._engine.report(task, outcome, supervisor=supervisor, attempt=attempt)

    def heartbeat(self, supervisor: str, tasks: Collection[int]) -> list[int]:
        """Renew the leases of `tasks`. Returns the tasks that the supervisor lost."""
        return self._engine.heartbeat(supervisor, tasks)

    def note(self, task: int, text: str, *, attempt: int | None = None) -> None:
        """One line of progress in the token's history."""
        self._engine.note(task, text, attempt=attempt)

    def set_memory(self, task: int, key: str, value: Any, *, attempt: int | None = None) -> None:
        """Save one key of the routine's memory for the task's token and state."""
        self._engine.set_memory(task, key, value, attempt=attempt)

    # ── the clock ───────────────────────────────────────────────────────

    def tick(self) -> int:
        """One step of the clock: renew the hold, then end expired leases and fire due
        timers. Returns how many. Raises `HoldLost` when another control plane took
        the database."""
        if not self._store.keep_serving():
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

    def close(self) -> None:
        """Stop the clock, give up the hold and let go of the connections."""
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
