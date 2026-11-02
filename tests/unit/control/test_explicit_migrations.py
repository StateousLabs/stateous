"""Migrations only when asked (`Store(..., migrate=False)`), and the notices setting."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from stateous.control.store import SchemaNotReady, Store
from stateous.control.store.migrations import head

from .support import sqlite_url


def test_an_unmigrated_namespace_is_refused_with_the_command(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "empty.db")

    with pytest.raises(SchemaNotReady) as refused:
        Store(url, migrate=False)

    assert str(refused.value) == (
        f"the database has no Stateous tables at version {head()}; run: stateous migrate --db URL"
    )
    engine = create_engine(url)
    try:
        assert inspect(engine).get_table_names() == []
    finally:
        engine.dispose()


def test_migrate_then_open_without_migrating(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "s.db")
    Store(url).close()

    store = Store(url, migrate=False)
    store.close()


def test_migrate_sets_and_keeps_the_notices_setting(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "s.db")
    store = Store(url)
    try:
        assert store.notices_on is False
        store.migrate(notices=True)
        store.migrate()
        assert store.notices_on is True
    finally:
        store.close()

    reopened = Store(url, migrate=False)
    try:
        assert reopened.notices_on is True
    finally:
        reopened.close()


def test_migrate_can_turn_notices_off_again(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "s.db")
    store = Store(url)
    try:
        store.migrate(notices=True)
        store.migrate(notices=False)
        assert store.notices_on is False
    finally:
        store.close()


def test_a_sqlite_engine_is_refused(tmp_path: Path) -> None:
    engine = create_engine(sqlite_url(tmp_path / "s.db"))
    try:
        with pytest.raises(ValueError, match="give SQLite as a URL; an engine is for Postgres"):
            Store(engine)
    finally:
        engine.dispose()
