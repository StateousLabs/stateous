"""Notices: what a developer's handler hears of a workflow. A notice is written in the
transaction of the change that it reports, so a change and its notice are both kept,
or neither is."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from stateous.control.store import Store, tables

log = logging.getLogger("stateous.notices")

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


class _HandlerFailed(Exception):
    """The handler raised: its family's transaction rolls back."""


def deliver(
    store: Store, workflow: str, handler: Callable[[Notice], None], *, limit: int = 100
) -> int:
    """Hand the waiting notices to `handler`, one family at a time, and delete each one
    that the handler has. Returns how many were delivered.

    A family's notices go in one transaction, under the family's notice lock. The
    handler runs inside it: a crash before the commit delivers them again. A handler
    that raises rolls its family back; those notices come again on the next call, in
    order, and the other families go on. A family that another deliverer holds is
    skipped. `workflow` is the main workflow's name: it starts each notice's id."""
    delivered = 0
    tried: set[str] = set()
    while delivered < limit:
        try:
            with store.transaction(datetime.now(UTC)) as tx:
                family = tx.next_notice_family(tried)
                if family is None:
                    break
                tried.add(family)
                if not tx.try_notice_lock(family):
                    continue
                handed = 0
                for row in tx.family_notices(family, limit - delivered):
                    try:
                        handler(_notice(row, workflow, store.namespace))
                    except Exception as error:
                        raise _HandlerFailed(family) from error
                    tx.delete_notice(row)
                    handed += 1
            delivered += handed
        except _HandlerFailed as failed:
            log.error(
                "the notice handler failed for the family %s (%s); its notices come again",
                failed.args[0],
                " ".join(str(failed.__cause__).split()) or type(failed.__cause__).__name__,
            )
    return delivered


def _notice(row: tables.Notice, workflow: str, namespace: str | None) -> Notice:
    return Notice(
        id=f"{workflow}:{namespace or '-'}:{row.id}",
        workflow=row.workflow,
        token=row.token,
        parent=row.parent,
        kind=cast("NoticeKind", row.kind),
        event=row.event,
        payload=None if row.payload is None else dict(row.payload),
        state=row.state,
        at=row.at.replace(tzinfo=UTC),
    )
