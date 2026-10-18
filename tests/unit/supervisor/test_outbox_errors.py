"""An outbox file that cannot be opened is refused with one sentence."""

from __future__ import annotations

from pathlib import Path

import pytest

from stateous.supervisor.outbox import Outbox, OutboxError


def test_a_folder_that_cannot_be_made_is_refused(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder", encoding="utf-8")

    with pytest.raises(OutboxError) as refused:
        Outbox.open(blocker / "sub" / "outbox.db")

    assert str(refused.value).startswith(f"cannot make {blocker / 'sub'}: ")


def test_a_path_that_is_a_folder_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "outbox.db"
    path.mkdir()

    with pytest.raises(OutboxError) as refused:
        Outbox.open(path)

    assert str(refused.value).startswith(f"cannot open {path}: ")


def test_a_file_that_is_not_a_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "outbox.db"
    path.write_text("this is not a SQLite file, and it is long enough to be read" * 20)

    with pytest.raises(OutboxError) as refused:
        Outbox.open(path)

    assert str(refused.value).startswith(f"cannot use {path}: ")
