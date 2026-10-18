"""Store edges: a hold that is gone, rows that are missing, and guards of the locks."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, update

from stateous.control.store import Store, tables

from .support import TICKET, holder_row, new_engine, sqlite_url

NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


def test_keep_serving_takes_the_hold_back_when_no_one_holds_the_database(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    url = sqlite_url(tmp_path / "s.db")
    store = Store(url, hold=True)
    try:
        engine = create_engine(url)
        with engine.begin() as connection:
            connection.execute(update(tables.ControlPlane).values(serving=None, since=None))
        engine.dispose()

        with caplog.at_level(logging.WARNING):
            assert store.keep_serving()

        assert holder_row(url)[0] == store.holder
        assert "this control plane took its hold on the database again" in caplog.text
    finally:
        store.close()


def test_a_store_that_cannot_clear_its_hold_on_close_says_so_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = Store(sqlite_url(tmp_path / "s.db"), hold=True)

    def gone(now: Any) -> Any:
        raise OSError("the database is gone")

    monkeypatch.setattr(store, "transaction", gone)

    with caplog.at_level(logging.WARNING):
        store.close()

    assert "could not clear this control plane's hold; it lapses by itself" in caplog.text


def test_a_token_with_no_accepted_event_has_no_last_accepted_entry() -> None:
    engine = new_engine()

    with engine.store.transaction(NOW) as tx:
        assert tx.last_accepted("NOPE") is None


def test_a_timer_that_does_not_exist_is_none() -> None:
    engine = new_engine()

    with engine.store.transaction(NOW) as tx:
        assert tx.timer(999) is None


def test_a_heartbeat_for_no_task_renews_nothing() -> None:
    engine = new_engine()
    engine.start("T-1", TICKET)

    assert engine.heartbeat("box-1", []) == []
