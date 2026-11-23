"""Purge on Postgres, beside a deliverer of notices. (Every test of
tests/unit/control/test_purge.py also runs on Postgres; see tests/conftest.py.)"""

from stateous.control.engine import Engine
from stateous.control.graph import Graph
from stateous.control.notices import Notice, deliver
from stateous.control.store import Store

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
            assert engine.purge(notice.token).tokens == 1

    deliver(store, "sdlc", handler)

    assert seen == ["event", "cancelled"]
    assert deliver(store, "sdlc", handler) == 0
    assert engine.purge("T-1").tokens == 0
    store.close()
