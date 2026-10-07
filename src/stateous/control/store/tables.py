"""The database schema, as SQLAlchemy models.

The ledger is the history: each change appends to it. The other tables say what is
true now. A change writes them in the same transaction as the ledger entries that
explain it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, DateTime, Index, Integer, String, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    # Times are UTC without a zone: SQLite drops zones, and one rule everywhere keeps
    # comparisons in SQL and in Python correct.
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSON,
        datetime: DateTime(),
    }


class LedgerEntry(Base):
    """One thing that happened to a token. Never changed after it is written."""

    __tablename__ = "ledger"
    __table_args__ = (Index("ledger_key", "token", "key", unique=True),)

    token: Mapped[str] = mapped_column(String(256), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    """1, 2, 3 … for each token. It orders entries where times are equal."""
    at: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(64))
    key: Mapped[str | None] = mapped_column(String(256))
    """The idempotency key of the entry, if it has one."""
    data: Mapped[dict[str, Any]]


class Token(Base):
    """Where each token is now, and its context."""

    __tablename__ = "tokens"
    __table_args__ = (Index("tokens_folded", func.lower(text("token"))),)
    """A new token whose id differs from another only in case is refused. The engine
    finds such a token by this index."""

    token: Mapped[str] = mapped_column(String(256), primary_key=True)
    state: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    """active · paused · completed · failed · cancelled"""
    visit: Mapped[int]
    """How many times the token has entered its current state."""
    context: Mapped[dict[str, Any]]
    entered_at: Mapped[datetime]


class Task(Base):
    """The task queue: one row for each task that entering a state makes."""

    __tablename__ = "tasks"
    __table_args__ = (Index("tasks_queue", "status", "state"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(64))
    visit: Mapped[int]
    status: Mapped[str] = mapped_column(String(16))
    """queued · claimed · waiting · done · failed · cancelled"""
    attempt: Mapped[int] = mapped_column(default=0)
    """How many times a supervisor claimed the task."""
    claimed_by: Mapped[str | None] = mapped_column(String(256), default=None)
    lease_until: Mapped[datetime | None] = mapped_column(default=None)
    """While a supervisor holds the task: when it is lost unless a heartbeat renews it."""
    queued_at: Mapped[datetime]


class Timer(Base):
    """Events for the clock to fire later: a state's timeout, or a routine's timer."""

    __tablename__ = "timers"
    __table_args__ = (Index("timers_due", "status", "due_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(64))
    """The state it was set in. Leaving the state cancels the timer."""
    visit: Mapped[int]
    event: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any] | None]
    set_at: Mapped[datetime]
    due_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(16))
    """pending · fired · cancelled"""
    kind: Mapped[str] = mapped_column(String(16))
    """timeout · timer"""


class Memory(Base):
    """The routine memory: one key and its value, for one token and one state."""

    __tablename__ = "memory"

    token: Mapped[str] = mapped_column(String(256), primary_key=True)
    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)


class ControlPlane(Base):
    """This control plane's own row: the graph on record, and who serves the database."""

    __tablename__ = "control_plane"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    """Always 1."""
    structure: Mapped[str | None] = mapped_column(String(80), default=None)
    checksum: Mapped[str | None] = mapped_column(String(80), default=None)
    serving: Mapped[str | None] = mapped_column(String(256), default=None)
    """The control plane that holds the database: `host:pid:id`."""
    since: Mapped[datetime | None] = mapped_column(default=None)
    """When that control plane last said that it still serves."""


class Claim(Base):
    """The last claim of each supervisor that named a request id, and what it got. A
    repeat of that claim, after its answer was lost, gets the same task again."""

    __tablename__ = "claims"

    supervisor: Mapped[str] = mapped_column(String(256), primary_key=True)
    request: Mapped[str] = mapped_column(String(64))
    """The request id that the supervisor sent with the claim. Empty: the row was
    made for a first claim, before that claim was kept."""
    task_id: Mapped[int | None] = mapped_column(default=None)
    """The task the claim got; None when there was no task."""
    attempt: Mapped[int | None] = mapped_column(default=None)
    """The attempt of that task that the claim started."""
