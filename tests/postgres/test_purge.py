"""Purge on Postgres, beside a deliverer of notices. (Every test of
tests/unit/control/test_purge.py also runs on Postgres; see tests/conftest.py.)"""

import logging
import threading

import pytest
from sqlalchemy import select

from stateous.control.engine import Engine, Purged
from stateous.control.graph import Graph
from stateous.control.notices import Notice, deliver
from stateous.control.store import Store, tables

from ..conftest import fresh_postgres_database, needs_postgres
from ..unit.control.support import TICKET, sdlc

pytestmark = needs_postgres


def test_a_handler_can_purge_the_token_whose_notices_it_has_now() -> None:
    """The handler purges the token at its `cancelled` notice. The purge deletes the
    family's notices while the deliverer still holds them: the deliverer finds
    nothing left to delete, and no notice of the token comes again."""
    store = Store(fresh_postgres_database(), namespace="flow")
    store.migrate(notices=True)
    engine = Engine(store, Graph(sdlc()))
    engine.start("T-1", TICKET)
    engine.fire("T-1", "done", None)
    engine.cancel("T-1")
    seen: list[str] = []

    def handler(notice: Notice) -> None:
        seen.append(notice.kind)
        if notice.kind == "cancelled":
            assert engine.purge(notice.token).purged == 1

    deliver(store, "sdlc", handler)

    assert seen == ["event", "cancelled"]
    assert deliver(store, "sdlc", handler) == 0
    assert engine.purge("T-1").purged == 0
    store.close()


def test_a_purge_never_waits_for_a_supervisors_claim_row(caplog: pytest.LogCaptureFixture) -> None:
    """A repeated claim locks the supervisor's `claims` row, then waits for the lock of
    the task's family. A purge holds that family lock: it must never wait for the
    `claims` row, or the two wait for each other. Here another connection holds the
    row, as a repeated claim does, for the whole purge."""
    store = Store(fresh_postgres_database(), namespace="flow")
    engine = Engine(store, Graph(sdlc()))
    engine.start("T-1", TICKET)
    grant = engine.claim("box", ["coding"], request="r-1")
    assert grant is not None
    engine.cancel("T-1")
    purged: list[Purged] = []
    caplog.set_level(logging.WARNING, logger="stateous.engine")

    with store.engine.connect() as claiming:
        claim_row = select(tables.Claim).where(tables.Claim.supervisor == "box")
        assert claiming.execute(claim_row.with_for_update()).one().task_id == grant.task
        purge = threading.Thread(target=lambda: purged.append(engine.purge("T-1")))
        purge.start()
        purge.join(timeout=10)
        finished_while_held = not purge.is_alive()
        claiming.rollback()
    purge.join(timeout=30)

    assert finished_while_held, "the purge waited for the claims row"
    assert purged == [Purged("T-1", 1)]
    assert not [r for r in caplog.records if "deadlock" in r.getMessage()]
    # The repeated claim finds its task gone, and is a new claim: there is no task.
    assert engine.claim("box", ["coding"], request="r-1") is None
    store.close()
