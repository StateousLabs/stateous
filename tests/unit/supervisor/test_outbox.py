"""The supervisor's outbox: a SQLite file of the messages not yet delivered."""

from __future__ import annotations

from pathlib import Path

import pytest

from stateous.supervisor.outbox import Message, Outbox, OutboxBusy, outbox_path


def test_the_outbox_is_in_the_folders_stateous_folder(tmp_path: Path) -> None:
    assert outbox_path(tmp_path) == tmp_path / ".stateous" / "outbox.db"


def test_open_makes_the_stateous_folder_and_the_file(tmp_path: Path) -> None:
    with Outbox.open(outbox_path(tmp_path)):
        assert (tmp_path / ".stateous" / "outbox.db").is_file()


def test_appended_messages_are_pending_in_order(tmp_path: Path) -> None:
    with Outbox.open(outbox_path(tmp_path)) as outbox:
        first = outbox.append(1, 2, "T-1", "memory", {"key": "dev", "value": "s-1"})
        second = outbox.append(1, 2, "T-1", "note", {"text": "one"})
        third = outbox.append(1, 2, "T-1", "report", {"kind": "event", "event": "done"})

        pending = outbox.pending()

    assert pending == [first, second, third]
    assert [message.kind for message in pending] == ["memory", "note", "report"]
    assert first.id < second.id < third.id
    assert (first.task, first.attempt, first.token) == (1, 2, "T-1")
    assert first.body == {"key": "dev", "value": "s-1"}
    assert first.created_at.endswith("+00:00")


def test_a_removed_message_is_no_longer_pending(tmp_path: Path) -> None:
    with Outbox.open(outbox_path(tmp_path)) as outbox:
        first = outbox.append(1, 1, "T-1", "note", {"text": "one"})
        second = outbox.append(1, 1, "T-1", "note", {"text": "two"})

        outbox.remove(first.id)

        assert outbox.pending() == [second]


def test_drop_removes_the_messages_of_one_attempt_only(tmp_path: Path) -> None:
    with Outbox.open(outbox_path(tmp_path)) as outbox:
        outbox.append(1, 1, "T-1", "note", {"text": "old"})
        kept = outbox.append(1, 2, "T-1", "note", {"text": "new"})
        other = outbox.append(2, 1, "T-2", "note", {"text": "other"})

        outbox.drop(1, 1)

        assert outbox.pending() == [kept, other]


def test_the_messages_stay_after_the_outbox_is_closed_and_opened_again(tmp_path: Path) -> None:
    path = outbox_path(tmp_path)
    with Outbox.open(path) as outbox:
        written = outbox.append(7, 3, "T-7", "report", {"kind": "waiting"})

    with Outbox.open(path) as again:
        assert again.pending() == [written]
        later = again.append(7, 3, "T-7", "note", {"text": "x"})
        assert later.id > written.id


def test_a_second_outbox_on_the_same_file_is_refused_while_the_first_is_open(
    tmp_path: Path,
) -> None:
    path = outbox_path(tmp_path)
    with Outbox.open(path), pytest.raises(OutboxBusy):
        Outbox.open(path)

    with Outbox.open(path) as later:  # the first one closed: the file is free
        assert later.pending() == []


def test_an_outbox_in_memory_works_the_same() -> None:
    with Outbox.in_memory() as outbox:
        message = outbox.append(1, 1, "T-1", "note", {"text": "one"})
        assert outbox.pending() == [message]
        assert isinstance(message, Message)
