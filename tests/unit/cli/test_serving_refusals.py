"""The refusals of `serve` for a host name, a port, a database and a stored graph."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import errno
import socket
from typing import Any

import pytest

from stateous.cli import serving
from stateous.cli.serving import ServeRefused
from stateous.control.store import ForeignDatabase


def test_a_host_name_is_bound_as_ipv4() -> None:
    listener = serving.bound("localhost", 0)
    try:
        assert listener.family == socket.AF_INET
    finally:
        listener.close()


def test_a_port_error_other_than_in_use_shows_the_system_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(host: str, port: int) -> socket.socket:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(serving, "bound", refuse)

    with pytest.raises(ServeRefused) as raised:
        serving._listen("127.0.0.1", 80)

    assert str(raised.value) == "cannot listen on 127.0.0.1:80: Permission denied"


def test_a_database_of_another_program_is_refused_in_one_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def foreign(db: str, *, hold: bool) -> object:
        raise ForeignDatabase("a table  has\nanother shape")

    monkeypatch.setattr(serving, "Store", foreign)

    with pytest.raises(ServeRefused) as raised:
        serving._open("postgresql://user:secret@host/db")

    assert str(raised.value) == (
        "cannot use the database postgresql://user:***@host/db: a table has another shape"
    )


def test_a_failure_while_reading_the_stored_graph_is_one_sentence() -> None:
    class Engine:
        graph = object()

        def recorded_graph(self) -> object:
            raise RuntimeError("the connection\nwas lost")

    engine: Any = Engine()

    with pytest.raises(ServeRefused) as raised:
        serving._check_structure(engine)

    assert str(raised.value) == "cannot use the database: the connection was lost"


def test_a_database_text_that_is_not_a_url_shows_as_it_is() -> None:
    assert serving._shown("not a url") == "not a url"
