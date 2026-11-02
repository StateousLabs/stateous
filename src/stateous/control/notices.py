"""Notices: what a developer's handler hears of a workflow. A notice is written in the
transaction of the change that it reports, so a change and its notice are both kept,
or neither is."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

NoticeKind = Literal["event", "completed", "failed", "cancelled", "task_failed"]
"""- `event`: an event was accepted (also a held event, when a resume applies it).
- `completed`, `failed`, `cancelled`: the token ended so.
- `task_failed`: a task failed for the last time; a failure that will retry gives none."""


@dataclass(frozen=True)
class Notice:
    """One change of a token, for the developer's handler."""

    id: str
    """Stable across deliveries: use it as an idempotency key."""
    workflow: str
    """The token's own workflow (a child token's is its child workflow)."""
    token: str
    parent: str | None
    """The token whose parallel state started this one; None for a top token."""
    kind: NoticeKind
    event: str | None
    """For `event`: the event's name."""
    payload: dict[str, Any] | None
    """For `event`: the payload as the event carried it."""
    state: str
    """For `event`: the state that the token moves to (`__end__` at the end). Else the
    state that the token is in."""
    at: datetime
