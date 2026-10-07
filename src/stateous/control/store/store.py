"""The store: one database, one transaction for each decision.

SQLite and Postgres. Nothing here decides: the engine does. This module reads and
writes inside a transaction that the engine opens, at a time that the engine gives.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from collections.abc import Collection, Generator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Connection,
    and_,
    create_engine,
    delete,
    event,
    false,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from stateous.control.store import tables
from stateous.control.store.errors import DatabaseHeld
from stateous.control.store.migrations import migrate

log = logging.getLogger("stateous.store")

SQLITE_WAIT = 30.0
"""Seconds a SQLite transaction waits for the write lock of another process, before
it fails with "database is locked". The driver's default is five."""

TOKEN_LOCK = "token:"
"""The start of each token's advisory-lock key: `token:<id in lower case>`."""

CANDIDATES = 20
"""How many queued tasks one claim looks at."""

SERVING_LOCK = "@stateous/serve"
"""The Postgres advisory-lock key of the serving hold. A token's key always starts
with `token:`, so the two can never be equal."""

SERVING_LEASE = timedelta(seconds=30)
"""SQLite: a hold that is not renewed for this long has lapsed, and another store
may take it."""

RENEW_EVERY = timedelta(seconds=10)
"""`keep_serving` writes `since` again when it is older than this."""


def database_url(url: str) -> str:
    """The URL that SQLAlchemy gets. Postgres URLs as people write them (postgres://
    or postgresql://) use psycopg 3, the driver that Stateous ships with."""
    for scheme in ("postgres://", "postgresql://"):
        if url.startswith(scheme):
            return "postgresql+psycopg://" + url.removeprefix(scheme)
    return url


def open_engine(url: str) -> Engine:
    """The SQLAlchemy engine of a store.

    On SQLite, SQLAlchemy (not the driver) says BEGIN, so that one transaction also
    covers schema changes: a migration that fails halfway changes nothing. It is
    BEGIN IMMEDIATE: the transaction takes the write lock at its start, so another
    process on the same file waits for its turn and does not fail at once."""
    url = database_url(url)
    sqlite = url.startswith("sqlite")
    in_memory = url in ("sqlite://", "sqlite:///:memory:")
    engine = create_engine(
        url,
        # An in-memory SQLite database lives in one connection; share it.
        poolclass=StaticPool if in_memory else None,
        connect_args={"check_same_thread": False, "timeout": SQLITE_WAIT} if sqlite else {},
    )
    if sqlite:
        event.listen(engine, "connect", _no_implicit_transactions)
        event.listen(engine, "begin", _begin)
    return engine


def _no_implicit_transactions(dbapi_connection: Any, _record: Any) -> None:
    dbapi_connection.isolation_level = None  # SQLAlchemy, not the driver, says BEGIN


def _begin(connection: Connection) -> None:
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def utc(moment: datetime) -> datetime:
    """A moment as the database keeps it: UTC, with no zone."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment


def wall_clock() -> datetime:
    """The real time. The serving hold is about real processes, not the engine's clock."""
    return datetime.now(UTC)


class Store:
    """A database that holds the ledger and the rows it explains."""

    def __init__(self, url: str, *, hold: bool = False) -> None:
        url = database_url(url)
        self.engine = open_engine(url)
        self.holder = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        """How this store names itself in the `control_plane` row when it serves."""
        self._serving: Connection | None = None
        self._holding = False
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        # SQLite takes one writer at a time, and an in-memory database is one shared
        # connection, so transactions from different threads take turns.
        self._turns: AbstractContextManager[Any] = (
            threading.RLock() if url.startswith("sqlite") else nullcontext()
        )
        try:
            # Postgres: refuse before the migration, so that a refused control plane
            # never changes the schema under the one that serves.
            if hold and self.is_postgres and not self._try_take():
                raise DatabaseHeld("another control plane serves this database; stop it first")
            migrate(self.engine)
            if hold and not self.hold_serving():
                raise DatabaseHeld(self._held_message())
        except BaseException:
            self.close()  # gives up the hold, if taken, and the connections
            raise

    @property
    def is_postgres(self) -> bool:
        return self.engine.dialect.name == "postgresql"

    def close(self) -> None:
        """Give up the hold, if this store has it, and let go of the connections."""
        if self._holding:
            self._holding = False
            try:
                with self.transaction(wall_clock()) as tx:
                    row = tx.control_plane()
                    if row.serving == self.holder:
                        row.serving, row.since = None, None
            except Exception:  # the database can be gone; then the hold lapses by itself
                log.warning("could not clear this control plane's hold; it lapses by itself")
        if self._serving is not None:
            self._serving.invalidate()  # its session ends, and the advisory lock with it
            self._serving.close()
            self._serving = None
        self.engine.dispose()

    def hold_serving(self) -> bool:
        """Take this database for this control plane, until `close()`. False if
        another control plane holds it. Postgres: a session advisory lock, on a
        connection set aside for it. SQLite: the `control_plane` row, which lapses
        when no one renews it for `SERVING_LEASE`. On both, the row says who holds
        the database, and since when."""
        if self.is_postgres and self._serving is None and not self._try_take():
            return False
        with self.transaction(wall_clock()) as tx:
            row = tx.control_plane()
            if not self.is_postgres and not _free(row, self.holder, tx.now):
                return False
            row.serving, row.since = self.holder, tx.now
        self._holding = True
        return True

    def keep_serving(self) -> bool:
        """Check the hold again, and renew it. Call it at least every `RENEW_EVERY`.
        On Postgres the lock's connection can die (the database restarted, the
        network dropped it): then the lock is tried again on a new connection. A
        hold that lapsed is taken back if no one else took it. False: another control
        plane serves the database now, and this one must stop."""
        if self.is_postgres and not self._keep_lock():
            self._holding = False
            return False
        with self.transaction(wall_clock()) as tx:
            row = tx.control_plane()
            if row.serving == self.holder and row.since is not None:
                if tx.now - row.since >= RENEW_EVERY:
                    row.since = tx.now
                return True
            if not self.is_postgres and not _free(row, self.holder, tx.now):
                self._holding = False
                return False
            row.serving, row.since = self.holder, tx.now
        log.warning("this control plane took its hold on the database again")
        self._holding = True
        return True

    def _held_message(self) -> str:
        with self.transaction(wall_clock()) as tx:
            row = tx.control_plane()
            who, since = row.serving, row.since
        if who is None or since is None:
            return "another control plane serves this database; stop it first"
        return (
            f"another control plane serves this database ({who}, last seen at "
            f"{since:%H:%M:%S} UTC); stop it first"
        )

    def _try_take(self) -> bool:
        """`pg_try_advisory_lock(SERVING_LOCK)` on a new connection set aside for it:
        `self._serving` when held; closed when not."""
        connection = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        held = bool(
            connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                {"key": SERVING_LOCK},
            )
        )
        if held:
            self._serving = connection
        else:
            connection.close()
        return held

    def _keep_lock(self) -> bool:
        """Postgres: whether this process still holds the advisory lock. A dead
        connection is dropped, and the lock is tried again on a new one."""
        if self._serving is not None:
            try:
                self._serving.scalar(text("SELECT 1"))
                return True
            except Exception:  # any failure means the connection is gone
                self._serving.invalidate()
                self._serving.close()
                self._serving = None
        return self._try_take()

    @contextmanager
    def transaction(self, now: datetime) -> Generator[Transaction]:
        """All that is done inside commits together, or not at all. `now` is the time
        of the transaction. Transactions do not nest."""
        with self._turns, self._sessions.begin() as session:
            yield Transaction(session, utc(now))


class Transaction:
    """Reads and writes inside one database transaction.

    On Postgres, many writers work at once. A transaction takes the lock of a token's
    family (`lock_token`) before it reads the token to change it, or appends to its
    ledger, and holds the lock until it ends. A family is a top token and the child
    tokens below it (design section 8). A parallel state changes a parent and its
    children in one transaction, so they share one lock: the lock of the top token.
    So the changes to one family take turns: a token moves one step at a time. A
    transaction waits for at most one family's lock, and only while it holds no other,
    so two transactions never wait for each other in a circle. A new claim never
    waits: it tries the lock, and skips a busy family. A repeated claim waits for the
    lock of the task it got before, and holds no other token's lock then. A claim
    with a request id also locks the supervisor's row in `claims` (`last_claim`). It
    takes that lock only before any token lock, and no transaction that holds a token
    lock waits for that row. So this order is safe too. SQLite takes one writer at a
    time and needs no locks.

    A heartbeat changes task rows without a token lock, in the order of their ids. A
    change to a family can change the tasks of several children in another order.
    Then Postgres finds a deadlock and stops one transaction. The engine runs a
    stopped call once more.
    """

    def __init__(self, session: Session, now: datetime) -> None:
        self._session = session
        self.now = now
        self._locks = session.get_bind().dialect.name == "postgresql"
        self._locked: set[str] = set()
        self._families: dict[str, str] = {}
        """Token id -> the id of its top token, as `family` found it or `adopt` set it."""

    def lock_token(self, token: str, *, wait: bool = True) -> bool:
        """Take the lock of the token's family until this transaction ends. Without
        `wait`, only if no one else holds it. True when it is held. The key ignores
        case, so `t-1` waits for `T-1` and then finds it (`token_folded`)."""
        if not self._locks:
            return True
        key = TOKEN_LOCK + self.family(token).lower()
        if key in self._locked:
            return True
        if wait and self._locked:
            raise RuntimeError("a transaction waits for a token's lock only when it holds none")
        function = "pg_advisory_xact_lock" if wait else "pg_try_advisory_xact_lock"
        held = self._session.scalar(
            text(f"SELECT {function}(hashtextextended(:key, 0))"), {"key": key}
        )
        if wait or held:
            self._locked.add(key)
            return True
        return False

    def family(self, token: str) -> str:
        """The id of the top token of `token`'s family: `token` itself, or the first
        token above it that has no parent. A token's `parent` never changes after the
        token is made, so this reads it without a lock. A token that is not there
        yet is its own top, unless `adopt` named its parent."""
        if token in self._families:
            return self._families[token]
        top = token
        while (
            above := self._session.scalar(
                select(tables.Token.parent).where(tables.Token.token == top)
            )
        ) is not None:
            top = above
        self._families[token] = top
        return top

    def adopt(self, child: str, parent: str) -> None:
        """Count the new token `child` in the family of `parent`. Call it before the
        child's first ledger entry: then the child's entries take the family's lock,
        which this transaction holds already."""
        self._families[child] = self.family(parent)

    # ── ledger ──────────────────────────────────────────────────────────

    def append(self, token: str, kind: str, /, *, key: str | None = None, **data: Any) -> int:
        """Record one entry. Returns its seq: the next number for this token. `token`
        and `kind` are positional only, so `data` can have a key `token` too."""
        self.lock_token(token)
        last = self._session.scalar(
            select(func.max(tables.LedgerEntry.seq)).where(tables.LedgerEntry.token == token)
        )
        seq = (last or 0) + 1
        self._session.add(
            tables.LedgerEntry(token=token, seq=seq, at=self.now, kind=kind, key=key, data=data)
        )
        self._session.flush()
        return seq

    def entries(self, token: str) -> list[tables.LedgerEntry]:
        return list(
            self._session.scalars(
                select(tables.LedgerEntry)
                .where(tables.LedgerEntry.token == token)
                .order_by(tables.LedgerEntry.seq)
            )
        )

    def find_key(self, token: str, key: str) -> tables.LedgerEntry | None:
        return self._session.scalar(
            select(tables.LedgerEntry).where(
                tables.LedgerEntry.token == token, tables.LedgerEntry.key == key
            )
        )

    def counts(self, token: str) -> dict[str, int]:
        """How many times each event was accepted on the token."""
        counts: dict[str, int] = {}
        for entry in self.entries(token):
            if entry.kind == "event_accepted":
                event_name = entry.data["event"]
                counts[event_name] = counts.get(event_name, 0) + 1
        return counts

    def visits(self, token: str, state: str) -> int:
        """How many times the token entered `state`."""
        return sum(
            1
            for entry in self.entries(token)
            if entry.kind == "entered" and entry.data.get("state") == state
        )

    def last_accepted(self, token: str) -> tables.LedgerEntry | None:
        for entry in reversed(self.entries(token)):
            if entry.kind == "event_accepted":
                return entry
        return None

    def arrival_payload(self, token: str) -> dict[str, Any] | None:
        """The payload of the event that brought the token into the last state it
        entered. For a completed token, that is the state it ended from."""
        entries = self.entries(token)
        entered = max((i for i, e in enumerate(entries) if e.kind == "entered"), default=0)
        for entry in reversed(entries[:entered]):
            if entry.kind == "event_accepted":
                payload: dict[str, Any] | None = entry.data.get("payload")
                return payload
        return None

    # ── tokens ──────────────────────────────────────────────────────────

    def token(self, token: str) -> tables.Token | None:
        """The token, read after its lock is taken."""
        self.lock_token(token)
        return self._session.get(tables.Token, token, populate_existing=True)

    def token_folded(self, token: str) -> str | None:
        """The id of a token that differs from `token` only in case, if there is one.
        Call it under the token's lock, which ignores case."""
        return self._session.scalar(
            select(tables.Token.token)
            .where(func.lower(tables.Token.token) == token.lower(), tables.Token.token != token)
            .limit(1)
        )

    def add_token(self, row: tables.Token) -> None:
        self._session.add(row)
        self._session.flush()

    def find_tokens(
        self, status: str | None, limit: int, *, parent: str | None = None
    ) -> list[tables.Token]:
        """Tokens, the most recently moved first; only those of `status`, if given; only
        the children of `parent`, if given."""
        query = select(tables.Token)
        if status is not None:
            query = query.where(tables.Token.status == status)
        if parent is not None:
            query = query.where(tables.Token.parent == parent)
        query = query.order_by(tables.Token.entered_at.desc(), tables.Token.token).limit(limit)
        return list(self._session.scalars(query))

    def count_live(self) -> int:
        """How many tokens are active or paused."""
        query = (
            select(func.count())
            .select_from(tables.Token)
            .where(tables.Token.status.in_(("active", "paused")))
        )
        return self._session.scalar(query) or 0

    def children(self, parent: str) -> list[tables.Token]:
        """The tokens that `parent` started, in the order of their ids. Call it under
        the parent's lock: it is the children's lock too."""
        return list(
            self._session.scalars(
                select(tables.Token)
                .where(tables.Token.parent == parent)
                .order_by(tables.Token.token)
            )
        )

    def token_taken(self, token: str) -> bool:
        """Whether a token has this id, compared without regard to case. No lock: the
        engine asks it before it makes a child, under the parent's lock."""
        found = self._session.scalar(
            select(tables.Token.token)
            .where(func.lower(tables.Token.token) == token.lower())
            .limit(1)
        )
        return found is not None

    # ── tasks ───────────────────────────────────────────────────────────

    def queue_task(self, token: str, state: str, visit: int) -> tables.Task:
        """A queued task. Its id is known on return."""
        task = tables.Task(
            token=token, state=state, visit=visit, status="queued", queued_at=self.now
        )
        self._session.add(task)
        self._session.flush()
        return task

    def task(self, task_id: int) -> tables.Task | None:
        """The task, read again after its token's lock is taken: what was read before
        the lock can have changed while this transaction waited for it."""
        task = self._session.get(tables.Task, task_id)
        if task is None:
            return None
        self.lock_token(task.token)
        return self._session.get(tables.Task, task_id, populate_existing=True)

    def open_tasks(self, token: str) -> list[tables.Task]:
        """The token's tasks that are queued, claimed or waiting."""
        return list(
            self._session.scalars(
                select(tables.Task)
                .where(
                    tables.Task.token == token,
                    tables.Task.status.in_(("queued", "claimed", "waiting")),
                )
                .order_by(tables.Task.id)
            )
        )

    def claim_task(self, supervisor: str, states: Collection[str]) -> tables.Task | None:
        """Claim the oldest queued task of one of `states` whose token is active and
        not busy. `<state>` names a state of the main workflow; `<workflow>/<state>`
        names a state of a child workflow. Two supervisors never take the same task."""
        candidates = self._session.execute(
            select(tables.Task.id, tables.Task.token)
            .join(tables.Token, tables.Token.token == tables.Task.token)
            .where(
                tables.Task.status == "queued",
                _served(states),
                tables.Token.status == "active",  # a paused token's task waits
            )
            .order_by(tables.Task.id)
            .limit(CANDIDATES)
        ).all()
        for task_id, token in candidates:
            if not self.lock_token(token, wait=False):
                continue  # its token is busy; the task is there next time
            task = self._session.get(tables.Task, task_id, populate_existing=True)
            row = self._session.get(tables.Token, token, populate_existing=True)
            if task is None or task.status != "queued" or row is None or row.status != "active":
                continue  # it changed after the first read
            task.status, task.claimed_by = "claimed", supervisor
            task.attempt += 1
            return task
        return None

    def last_claim(self, supervisor: str) -> tables.Claim:
        """The last claim of `supervisor` that named a request id. Locked on Postgres
        until this transaction ends. Call it before any token lock.

        A supervisor with no claim yet gets a row with an empty request id, which no
        request id is equal to. The row is made first, so that the lock has a row to
        lock: two first claims with the same request id take turns, and the second
        is a repeat of the first."""
        if self._locked:
            raise RuntimeError("a transaction locks a claim only when it holds no token lock")
        dialect = postgresql if self._locks else sqlite
        self._session.execute(
            dialect.insert(tables.Claim)
            .values(supervisor=supervisor, request="")
            .on_conflict_do_nothing(index_elements=["supervisor"])
        )
        row = self._session.get(
            tables.Claim, supervisor, populate_existing=True, with_for_update=self._locks
        )
        assert row is not None, "the claim row was made above"
        return row

    def remember_claim(self, supervisor: str, request: str, task: tables.Task | None) -> None:
        """Keep `request` and what it got (a task and its attempt, or no task) as the
        last claim of `supervisor`. It replaces the claim kept before."""
        self._session.merge(
            tables.Claim(
                supervisor=supervisor,
                request=request,
                task_id=None if task is None else task.id,
                attempt=None if task is None else task.attempt,
            )
        )
        self._session.flush()

    def renew_leases(self, supervisor: str, ids: Collection[int], until: datetime) -> set[int]:
        """Move the lease of each task in `ids` that `supervisor` still holds (claimed,
        by it, with no lease yet, or a lease that has not yet ended) to `until`.
        Returns those ids. A task whose lease already ended is not renewed: it is
        reported lost, even before the sweep that frees it runs. No token lock: one
        statement, so a heartbeat never waits for a token."""
        if not ids:
            return set()
        renewed = self._session.scalars(
            update(tables.Task)
            .where(
                tables.Task.id.in_(sorted(ids)),
                tables.Task.claimed_by == supervisor,
                tables.Task.status == "claimed",
                or_(tables.Task.lease_until.is_(None), tables.Task.lease_until >= self.now),
            )
            .values(lease_until=until)
            .returning(tables.Task.id)
            .execution_options(synchronize_session=False)
        )
        return set(renewed)

    def end_lease(self, task_id: int) -> bool:
        """End the lease of a claimed task, only if it is still over now. False if a
        heartbeat renewed it meanwhile. One guarded statement: on Postgres it waits
        for a heartbeat that changes the row, then checks the new lease. A heartbeat
        that comes later waits for this transaction, and finds the task no longer
        claimed."""
        ended = self._session.scalars(
            update(tables.Task)
            .where(
                tables.Task.id == task_id,
                tables.Task.status == "claimed",
                tables.Task.lease_until.is_not(None),
                tables.Task.lease_until < self.now,
            )
            .values(lease_until=None)
            .returning(tables.Task.id)
            .execution_options(synchronize_session=False)
        )
        return ended.first() is not None

    def expired_leases(self) -> list[int]:
        """The ids of the claimed tasks whose leases ended."""
        return list(
            self._session.scalars(
                select(tables.Task.id)
                .where(
                    tables.Task.status == "claimed",
                    tables.Task.lease_until.is_not(None),
                    tables.Task.lease_until < self.now,
                )
                .order_by(tables.Task.id)
            )
        )

    # ── timers ──────────────────────────────────────────────────────────

    def add_timer(
        self,
        token: str,
        state: str,
        visit: int,
        event: str,
        payload: dict[str, Any] | None,
        due_at: datetime,
        *,
        kind: str,
    ) -> tables.Timer:
        """A pending timer, set now. Its id is known on return."""
        timer = tables.Timer(
            token=token, state=state, visit=visit, event=event, payload=payload,
            set_at=self.now, due_at=due_at, status="pending", kind=kind,
        )  # fmt: skip
        self._session.add(timer)
        self._session.flush()
        return timer

    def pending_timers(self, token: str) -> list[tables.Timer]:
        return list(
            self._session.scalars(
                select(tables.Timer)
                .where(tables.Timer.token == token, tables.Timer.status == "pending")
                .order_by(tables.Timer.id)
            )
        )

    def due_timers(self) -> list[int]:
        """The ids of the pending timers that are due now, of active tokens."""
        return list(
            self._session.scalars(
                select(tables.Timer.id)
                .join(tables.Token, tables.Token.token == tables.Timer.token)
                .where(
                    tables.Timer.status == "pending",
                    tables.Timer.due_at <= self.now,
                    tables.Token.status == "active",
                )
                .order_by(tables.Timer.due_at, tables.Timer.id)
            )
        )

    def timer(self, timer_id: int) -> tables.Timer | None:
        """The timer, read again after its token's lock is taken."""
        timer = self._session.get(tables.Timer, timer_id)
        if timer is None:
            return None
        self.lock_token(timer.token)
        return self._session.get(tables.Timer, timer_id, populate_existing=True)

    # ── memory ──────────────────────────────────────────────────────────

    def memory(self, token: str, state: str) -> dict[str, Any]:
        rows = self._session.scalars(
            select(tables.Memory).where(tables.Memory.token == token, tables.Memory.state == state)
        )
        return {row.key: row.value for row in rows}

    def set_memory(self, token: str, state: str, key: str, value: Any) -> None:
        self._session.merge(tables.Memory(token=token, state=state, key=key, value=value))
        self._session.flush()

    def forget(self, token: str, state: str, key: str) -> bool:
        """Delete one key. False if it was not there."""
        row = self._session.get(tables.Memory, (token, state, key))
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    def delete_memory(self, token: str) -> None:
        """Delete all of the token's memory, of every state."""
        self._session.execute(delete(tables.Memory).where(tables.Memory.token == token))

    # ── the control plane ───────────────────────────────────────────────

    def control_plane(self) -> tables.ControlPlane:
        """The control plane's one row, locked on Postgres until this transaction ends."""
        row = self._session.get(
            tables.ControlPlane, 1, populate_existing=True, with_for_update=self._locks
        )
        if row is None:  # the migrations seed it; this is only a safety net
            row = tables.ControlPlane(id=1)
            self._session.add(row)
            self._session.flush()
        return row


def _free(row: tables.ControlPlane, holder: str, now: datetime) -> bool:
    """SQLite: whether `holder` may take the database. Yes when no one holds it, when
    `holder` holds it, or when the hold lapsed."""
    if row.serving is None or row.serving == holder or row.since is None:
        return True
    return row.since + SERVING_LEASE < now


def _served(states: Collection[str]) -> ColumnElement[bool]:
    """The tasks of `states`: `<state>` for the main workflow, whose tokens have no
    workflow; `<workflow>/<state>` for a child workflow."""
    by_workflow: dict[str, set[str]] = {}
    for name in states:
        workflow, _, state = name.rpartition("/")
        by_workflow.setdefault(workflow, set()).add(state)
    found: list[ColumnElement[bool]] = []
    for workflow, names in sorted(by_workflow.items()):
        own = tables.Token.workflow.is_(None) if not workflow else tables.Token.workflow == workflow
        found.append(and_(own, tables.Task.state.in_(sorted(names))))
    return or_(*found) if found else false()
