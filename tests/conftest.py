"""Shared test data, and running the tests against Postgres.

Set STATEOUS_TEST_POSTGRES to a server, for example

    STATEOUS_TEST_POSTGRES=postgresql://stateous:stateous@localhost:55432/postgres

Then each in-memory store that a test opens is a new Postgres database, and the
tests marked `needs_postgres` run. Without it, nothing changes, and they are skipped.
"""

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from stateous import END, START, Workflow
from stateous.compiler.compile import compile_workflow
from stateous.control.store import Store
from stateous.control.store.store import database_url


class Ticket(BaseModel):
    repo: str


class Tests(BaseModel):
    passed: int
    failed: int


def sdlc_workflow() -> Workflow:
    """The workflow of design section 4.1, as it is written there."""
    flow = Workflow("sdlc", context=Ticket)
    coding = flow.state("coding", timeout="6h", retries=2)
    testing = flow.state("testing", timeout="1h")
    done = flow.event("done")
    stuck = flow.event("stuck")
    passed = flow.event("passed", payload=Tests)
    failed = flow.event("failed", payload=Tests)
    flow.transition(START, coding)
    flow.transition(coding, testing, on=done)
    flow.transition(coding, END, on=stuck)
    flow.transition(testing, END, on=passed)
    flow.transition(testing, coding, on=failed, when="count('failed') < 3")
    flow.transition(testing, END, on=failed, otherwise=True)
    return flow


@pytest.fixture
def sdlc() -> Workflow:
    return sdlc_workflow()


@pytest.fixture
def sdlc_document() -> dict[str, Any]:
    return compile_workflow(sdlc_workflow())


# ── the Postgres switch ────────────────────────────────────────────────────

POSTGRES = os.environ.get("STATEOUS_TEST_POSTGRES")

_created_databases: list[str] = []
"""Databases that `fresh_postgres_database` made and no one dropped yet."""


def fresh_postgres_database() -> str:
    """A new, empty database on the test server; its URL."""
    assert POSTGRES is not None
    admin = create_engine(database_url(POSTGRES), isolation_level="AUTOCOMMIT")
    name = f"t_{uuid.uuid4().hex[:16]}"
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    _created_databases.append(name)
    return make_url(POSTGRES).set(database=name).render_as_string(hide_password=False)


def drop_postgres_database(name: str) -> None:
    """Drop a database that `fresh_postgres_database` made, and disconnect all users."""
    assert POSTGRES is not None
    admin = create_engine(database_url(POSTGRES), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture(autouse=True)
def _stores_on_postgres(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if POSTGRES is None:
        yield
        return
    original = Store.__init__
    opened: list[Store] = []

    def on_postgres(self: Store, url: str, **options: Any) -> None:
        if url in ("sqlite://", "sqlite:///:memory:"):
            url = fresh_postgres_database()
        original(self, url, **options)
        opened.append(self)

    monkeypatch.setattr(Store, "__init__", on_postgres)
    yield
    # Close the stores before their databases are dropped. Closing a store twice is
    # harmless.
    for store in opened:
        store.close()
    while _created_databases:
        drop_postgres_database(_created_databases.pop())


def straggler_databases() -> list[str]:
    """Databases still named `t_<hex>` on the test server, never a template."""
    assert POSTGRES is not None
    admin = create_engine(database_url(POSTGRES), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        names = (
            connection.execute(
                text(
                    "SELECT datname FROM pg_database "
                    "WHERE datname LIKE 't\\_%' ESCAPE '\\' AND NOT datistemplate"
                )
            )
            .scalars()
            .all()
        )
    admin.dispose()
    return list(names)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    """A safety net for a test session that stopped before its stores were closed."""
    if POSTGRES is None:
        return
    for name in straggler_databases():
        drop_postgres_database(name)


needs_postgres = pytest.mark.skipif(POSTGRES is None, reason="set STATEOUS_TEST_POSTGRES")
