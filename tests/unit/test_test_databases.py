"""The test session's Postgres safety net drops only the databases it made."""

from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest


def shared_conftest(config: pytest.Config) -> ModuleType:
    """`tests/conftest.py`, as pytest loaded it."""
    root = Path(__file__).resolve().parents[1] / "conftest.py"
    for plugin in config.pluginmanager.get_plugins():
        where = getattr(plugin, "__file__", None)
        if isinstance(plugin, ModuleType) and where is not None and Path(where) == root:
            return plugin
    raise AssertionError("pytest did not load tests/conftest.py")


def test_the_session_drops_only_the_databases_it_created(
    pytestconfig: pytest.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    conftest = shared_conftest(pytestconfig)
    dropped: list[str] = []

    def drop(name: str) -> None:
        dropped.append(name)

    def everything_on_the_server() -> list[str]:
        return ["t_mine", "t_someone_elses"]

    monkeypatch.setattr(conftest, "POSTGRES", "postgresql://test-server/postgres")
    monkeypatch.setattr(conftest, "_created_databases", ["t_mine"])
    monkeypatch.setattr(conftest, "drop_postgres_database", drop)
    monkeypatch.setattr(conftest, "straggler_databases", everything_on_the_server, raising=False)

    finish: Any = conftest.pytest_sessionfinish
    finish(cast(pytest.Session, None), 0)

    assert dropped == ["t_mine"]
    assert conftest._created_databases == []


def test_the_test_server_on_localhost_is_reached_on_ipv4(pytestconfig: pytest.Config) -> None:
    """`localhost` is `::1` first on Windows, and Docker Desktop's relay there can
    lose bytes (tests/postgres/test_connection.py). The tests use 127.0.0.1."""
    conftest = shared_conftest(pytestconfig)
    server_url: Any = conftest.server_url

    assert (
        server_url("postgresql://stateous:secret@localhost:55432/postgres")
        == "postgresql://stateous:secret@127.0.0.1:55432/postgres"
    )
    assert (
        server_url("postgresql://stateous:secret@db.example:5432/postgres")
        == "postgresql://stateous:secret@db.example:5432/postgres"
    )
