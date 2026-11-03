"""Replicas are for Postgres: SQLite refuses them."""

from pathlib import Path

import pytest

from stateous import ControlPlane

from .support import sdlc, sqlite_url


def test_sqlite_refuses_replicas(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="replicas need Postgres"):
        ControlPlane(sdlc(), database=sqlite_url(tmp_path / "s.db"), migrate=True, exclusive=False)
