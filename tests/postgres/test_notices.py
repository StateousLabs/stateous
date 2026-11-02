"""Notices on Postgres: two deliverers on one namespace never hand one notice to both,
and each family's notices arrive in order."""

import threading
from collections import defaultdict

from stateous.control.engine import Engine
from stateous.control.graph import Graph
from stateous.control.notices import Notice, deliver
from stateous.control.store import Store

from ..conftest import fresh_postgres_database, needs_postgres
from ..unit.control.support import TICKET, sdlc

pytestmark = needs_postgres

FAMILIES = 20
FAILED = {"passed": 2, "failed": 1}
PASSED = {"passed": 3, "failed": 0}
STEPS: list[tuple[str, dict[str, int] | None]] = [
    ("done", None),
    ("failed", FAILED),
    ("done", None),
    ("failed", FAILED),
    ("done", None),
    ("passed", PASSED),
]
EXPECTED = [event for event, _ in STEPS] + ["completed"]


def test_two_deliverers_never_hand_one_notice_to_both() -> None:
    url = fresh_postgres_database()
    store = Store(url, namespace="flow")
    store.migrate(notices=True)
    engine = Engine(store, Graph(sdlc()))
    for family in range(FAMILIES):
        token = f"T-{family}"
        engine.start(token, TICKET)
        for event, payload in STEPS:
            engine.fire(token, event, payload)
    store.close()

    by_id: dict[str, list[str]] = defaultdict(list)
    by_token: dict[str, list[str]] = defaultdict(list)
    lock = threading.Lock()
    errors: list[BaseException] = []

    def handler(notice: Notice) -> None:
        with lock:
            by_id[notice.id].append(threading.current_thread().name)
            by_token[notice.token].append(notice.event or notice.kind)

    def run() -> None:
        own = Store(url, namespace="flow", migrate=False)
        try:
            while deliver(own, "sdlc", handler, limit=7):
                pass
        except BaseException as error:  # collected and checked below
            errors.append(error)
        finally:
            own.close()

    threads = [threading.Thread(target=run, name=f"deliverer-{n}") for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert len(by_id) == FAMILIES * len(EXPECTED)
    assert all(len(names) == 1 for names in by_id.values())
    assert all(events == EXPECTED for events in by_token.values())
