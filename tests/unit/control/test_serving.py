"""One control plane serves one database. These tests use SQLite files; the
Postgres lock is tested in tests/postgres/test_serving.py."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, update

from stateous.control.engine import TOKEN_ID_PATTERN, Engine, InvalidTokenId
from stateous.control.graph import Graph
from stateous.control.store import DatabaseHeld, Store, tables
from stateous.control.store.store import SERVING_LOCK

from .support import TICKET, sdlc


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def holder_row(url: str) -> tuple[str | None, datetime | None]:
    engine = create_engine(url)
    with engine.connect() as connection:
        row = connection.execute(
            select(tables.ControlPlane.serving, tables.ControlPlane.since)
        ).one()
    engine.dispose()
    return row[0], row[1]


def age_the_hold(url: str, by: timedelta) -> None:
    """Move the hold's `since` back, as if no one had renewed it for `by`."""
    _, since = holder_row(url)
    assert since is not None
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(update(tables.ControlPlane).values(since=since - by))
    engine.dispose()


def test_a_store_that_holds_the_database_says_who_it_is(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "s.db")
    store = Store(url, hold=True)
    try:
        serving, since = holder_row(url)
        assert serving == store.holder
        assert since is not None
    finally:
        store.close()

    assert holder_row(url) == (None, None)


def test_a_store_that_does_not_hold_changes_nothing(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "s.db")

    Store(url).close()

    assert holder_row(url) == (None, None)


def test_a_second_store_is_refused_while_the_first_holds_the_database(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "s.db")
    first = Store(url, hold=True)
    try:
        with pytest.raises(DatabaseHeld, match="another control plane serves this database"):
            Store(url, hold=True)
    finally:
        first.close()

    Store(url, hold=True).close()  # the first stopped: the next one starts at once


def test_a_hold_that_is_not_renewed_lapses_and_another_store_takes_it(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "s.db")
    crashed = Store(url, hold=True)  # it stops without close()
    age_the_hold(url, timedelta(seconds=31))

    second = Store(url, hold=True)
    try:
        assert not crashed.keep_serving()  # another control plane has it now
        assert second.keep_serving()
    finally:
        second.close()
        crashed.engine.dispose()


class FakeClock:
    """A stand-in for `wall_clock` that moves only when a test tells it to. Some
    assertions depend on how much simulated time passed since the hold, or since its
    last renewal; the real clock must not be free to add its own, unplanned time."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, by: timedelta) -> None:
        self._now += by


def test_keep_serving_renews_the_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = sqlite_url(tmp_path / "s.db")
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    monkeypatch.setattr("stateous.control.store.store.wall_clock", clock)

    store = Store(url, hold=True)
    try:
        clock.advance(timedelta(seconds=20))
        assert store.keep_serving()  # 20s since the hold: past RENEW_EVERY, so it renews
        clock.advance(timedelta(seconds=20))  # 40s since the hold, 20s since the renewal

        with pytest.raises(DatabaseHeld):
            Store(url, hold=True)
    finally:
        store.close()


def test_keep_serving_takes_a_lapsed_hold_back_when_no_one_else_took_it(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "s.db")
    store = Store(url, hold=True)
    try:
        age_the_hold(url, timedelta(minutes=5))

        assert store.keep_serving()
        assert holder_row(url)[0] == store.holder
    finally:
        store.close()


def test_the_serving_lock_s_key_can_never_be_a_token_id() -> None:
    """The token-id rule already keeps a token from colliding with the serving
    lock's advisory-lock key. No real database is needed to show it."""
    assert not re.fullmatch(TOKEN_ID_PATTERN, SERVING_LOCK)

    engine = Engine(Store("sqlite://"), Graph(sdlc()))
    with pytest.raises(InvalidTokenId):
        engine.start(SERVING_LOCK, TICKET)
