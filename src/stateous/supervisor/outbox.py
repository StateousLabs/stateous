"""The supervisor's outbox: the reports, notes and memory writes that the control
plane did not answer yet (design section 7).

The outbox is one SQLite file, `<folder>/.stateous/outbox.db`. The supervisor
writes each message to it first, then sends it. A message leaves the outbox when
the control plane answers it for good. Each write commits at once, so a message
is on disk before its HTTP call starts.

The outbox holds an exclusive lock on its file while it is open. A second
supervisor on the same folder cannot open it: two supervisors that share one
outbox would send each other's messages. Use an outbox from one thread at a time.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self, cast

Kind = Literal["report", "note", "memory"]

STATE_FOLDER = ".stateous"
OUTBOX_FILE = "outbox.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    token TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('report', 'note', 'memory')),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


class OutboxError(Exception):
    """The outbox cannot be opened. The message is one plain sentence."""


class OutboxBusy(OutboxError):
    """Another outbox has the file open: another supervisor runs on the folder."""


def outbox_path(folder: Path) -> Path:
    """The outbox file of the supervisor's folder `folder`."""
    return folder / STATE_FOLDER / OUTBOX_FILE


@dataclass(frozen=True)
class Message:
    """One message in the outbox."""

    id: int
    """The order of the messages: a later message has a higher id."""
    task: int
    attempt: int
    token: str
    kind: Kind
    body: dict[str, Any]
    """A report: the outcome (`Outcome.to_json`). A note: `{"text"}`. A memory
    write: `{"key", "value"}`."""
    created_at: str


class Outbox:
    """The messages not yet delivered, in a SQLite database."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    @classmethod
    def open(cls, path: Path) -> Outbox:
        """Open the outbox file at `path`. Make the file and its folder if they are
        absent. Raise `OutboxBusy` when another outbox has the file open."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise OutboxError(f"cannot make {path.parent}: {error}") from None
        return cls._connect(str(path))

    @classmethod
    def in_memory(cls) -> Outbox:
        """An outbox that is not on disk: its messages end with the process. For tests."""
        return cls._connect(":memory:")

    @classmethod
    def _connect(cls, where: str) -> Outbox:
        try:
            connection = sqlite3.connect(
                where, timeout=0, isolation_level=None, check_same_thread=False
            )
        except sqlite3.Error as error:
            raise OutboxError(f"cannot open {where}: {error}") from None
        try:
            connection.execute("PRAGMA locking_mode = EXCLUSIVE")
            connection.execute("BEGIN EXCLUSIVE")  # the lock stays until the connection closes
            connection.execute(_SCHEMA)
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            connection.close()
            if "locked" in str(error):
                raise OutboxBusy(f"{where} is open in another process") from None
            raise OutboxError(f"cannot use {where}: {error}") from None
        return cls(connection)

    def append(self, task: int, attempt: int, token: str, kind: Kind, body: Any) -> Message:
        """Write one message, and commit it. Returns it with its id."""
        created_at = datetime.now(UTC).isoformat()
        text = json.dumps(body, allow_nan=False)
        cursor = self._db.execute(
            "INSERT INTO messages (task, attempt, token, kind, body, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task, attempt, token, kind, text, created_at),
        )
        number = cursor.lastrowid
        assert number is not None
        return Message(number, task, attempt, token, kind, json.loads(text), created_at)

    def pending(self) -> list[Message]:
        """All the messages, in the order they were written."""
        rows = self._db.execute(
            "SELECT id, task, attempt, token, kind, body, created_at FROM messages ORDER BY id"
        ).fetchall()
        return [
            Message(
                int(row[0]),
                int(row[1]),
                int(row[2]),
                str(row[3]),
                cast("Kind", row[4]),
                json.loads(row[5]),
                str(row[6]),
            )
            for row in rows
        ]

    def remove(self, message: int) -> None:
        """Remove the message with the id `message`: the control plane answered it."""
        self._db.execute("DELETE FROM messages WHERE id = ?", (message,))

    def drop(self, task: int, attempt: int) -> None:
        """Remove the messages of that attempt of the task: the control plane took
        the task back, so it refuses them."""
        self._db.execute("DELETE FROM messages WHERE task = ? AND attempt = ?", (task, attempt))

    def close(self) -> None:
        """Close the file, and release its lock."""
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
