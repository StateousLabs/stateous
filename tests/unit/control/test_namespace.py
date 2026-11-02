"""The namespace of a store: its name rules, and SQLite's refusal."""

from pathlib import Path

import pytest

from stateous.control.store import Store
from stateous.control.store.store import check_namespace, lock_key

from .support import sqlite_url

NAME_RULE = "a namespace is a lower-case letter, then up to 62 lower-case letters, digits or '_'"


@pytest.mark.parametrize("name", ["Plan", "1x", "a-b", "x;drop", "a" * 64, ""])
def test_a_bad_namespace_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match=NAME_RULE):
        check_namespace(name)


def test_a_good_namespace_is_kept_as_it_is() -> None:
    assert check_namespace("plan_tasks") == "plan_tasks"
    assert check_namespace("a" * 63) == "a" * 63


def test_sqlite_refuses_a_namespace(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="a namespace is for Postgres"):
        Store(sqlite_url(tmp_path / "s.db"), namespace="plan_tasks")


def test_a_sqlite_store_has_no_namespace(tmp_path: Path) -> None:
    store = Store(sqlite_url(tmp_path / "s.db"))
    try:
        assert store.namespace is None
    finally:
        store.close()


def test_a_lock_key_starts_with_its_namespace() -> None:
    assert lock_key("plan", "token:t-1") == "plan/token:t-1"
    assert lock_key(None, "token:t-1") == "token:t-1"
