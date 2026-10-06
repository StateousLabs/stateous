"""`prepare_serve`: it checks --listen, the graph file, the port, the database's
hold and the structure on record. Each refusal is one plain sentence (contract
section 15). The server that it prepares runs the API and the clock."""

import copy
import json
import socket
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from sqlalchemy import create_engine, update

from stateous import Client
from stateous.cli import serving as serving_module
from stateous.cli.serving import GracefulServer, ServeRefused, Serving, prepare_serve
from stateous.control.engine import Engine, RecordedGraph
from stateous.control.graph import Graph
from stateous.control.store import Store, tables


def write(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def free() -> str:
    """`127.0.0.1:<a port that is free now>`."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{probe.getsockname()[1]}"


def until(condition: Callable[[], bool], within: float = 10.0) -> None:
    deadline = time.monotonic() + within
    while not condition():
        assert time.monotonic() < deadline, "the condition did not come true in time"
        time.sleep(0.02)


@contextmanager
def running(serving: Serving) -> Generator[str]:
    """Serve `serving` in a thread, as `stateous serve` does. Its URL. Then stop the
    server and close `serving`."""
    server = uvicorn.Server(serving.config)
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [serving.listener]}, daemon=True
    )
    thread.start()
    try:
        until(lambda: server.started or not thread.is_alive())
        assert server.started, "the control plane did not start"
        yield serving.url
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        serving.close()


@contextmanager
def database_of(db: str, document: dict[str, Any]) -> Generator[Engine]:
    """An engine on `db`, without the hold. A test uses it to put the database in the
    state that an earlier `serve` left."""
    store = Store(db)
    try:
        yield Engine(store, Graph(document))
    finally:
        store.close()


@pytest.fixture
def db(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'stateous.db').as_posix()}"


def test_port_0_gets_a_free_port_and_serves_the_api(
    tmp_path: Path, sdlc: dict[str, Any], db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    serving = prepare_serve(write(tmp_path / "sdlc.json", sdlc), db, "127.0.0.1:0")
    port = serving.listener.getsockname()[1]

    assert port != 0
    assert serving.url == f"http://127.0.0.1:{port}"
    assert capsys.readouterr().err == ""  # a local listen: no warning
    with running(serving) as url, Client(url) as client:
        assert client.start("T-1", {"repo": "acme/api"})["state"] == "coding"


def test_health_answers_the_checksum_of_the_graph(
    tmp_path: Path, sdlc: dict[str, Any], db: str
) -> None:
    serving = prepare_serve(write(tmp_path / "sdlc.json", sdlc), db, "127.0.0.1:0")

    with running(serving) as url, httpx2.Client(timeout=5) as http:
        health = http.get(f"{url}/health").json()

    assert health == {"workflow": "sdlc", "checksum": Graph(sdlc).checksum}


def test_the_clock_fires_a_timeout(tmp_path: Path, sdlc: dict[str, Any], db: str) -> None:
    sdlc["states"]["coding"]["timeout"] = "1s"
    serving = prepare_serve(write(tmp_path / "sdlc.json", sdlc), db, "127.0.0.1:0")

    with running(serving) as url, Client(url) as client:
        client.start("T-1", {"repo": "acme/api"})
        until(lambda: client.get("T-1")["status"] == "failed")


def test_a_second_serve_on_the_same_database_is_refused(
    tmp_path: Path, sdlc: dict[str, Any], db: str
) -> None:
    graph = write(tmp_path / "sdlc.json", sdlc)
    first = prepare_serve(graph, db, "127.0.0.1:0")
    try:
        with pytest.raises(ServeRefused, match="serves this database"):
            prepare_serve(graph, db, "127.0.0.1:0")
    finally:
        first.close()

    prepare_serve(graph, db, "127.0.0.1:0").close()  # the hold was given up: the next starts


def test_a_changed_structure_is_refused_while_tokens_are_live(
    tmp_path: Path, sdlc: dict[str, Any], db: str
) -> None:
    changed = copy.deepcopy(sdlc)
    changed["transitions"][4] = {"from": "testing", "to": "__end__", "on": "failed"}
    new = write(tmp_path / "new.json", changed)
    with database_of(db, sdlc) as engine:
        engine.record_graph()
        engine.start("T-1", {"repo": "acme/api"})

    with pytest.raises(ServeRefused, match="refusing to serve: 1 token"):
        prepare_serve(new, db, "127.0.0.1:0")

    with database_of(db, sdlc) as engine:
        engine.cancel("T-1")
    prepare_serve(new, db, "127.0.0.1:0").close()
    with database_of(db, changed) as engine:
        on_record = RecordedGraph(engine.graph.structure, engine.graph.checksum)
        assert engine.recorded_graph() == on_record


def test_the_same_structure_with_another_checksum_is_recorded_and_served(
    tmp_path: Path, sdlc: dict[str, Any], db: str
) -> None:
    with database_of(db, sdlc) as engine:
        engine.record_graph()
        engine.start("T-1", {"repo": "acme/api"})
    sdlc["states"]["coding"]["timeout"] = "2h"

    prepare_serve(write(tmp_path / "new.json", sdlc), db, "127.0.0.1:0").close()

    with database_of(db, sdlc) as engine:
        on_record = RecordedGraph(engine.graph.structure, engine.graph.checksum)
        assert engine.recorded_graph() == on_record
        assert engine.get("T-1").status == "active"


def test_a_port_in_use_is_refused_before_the_database_is_touched(
    tmp_path: Path, sdlc: dict[str, Any]
) -> None:
    graph = write(tmp_path / "sdlc.json", sdlc)
    database = tmp_path / "s.db"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        with pytest.raises(ServeRefused, match="already uses that port"):
            prepare_serve(graph, f"sqlite:///{database.as_posix()}", f"127.0.0.1:{port}")

    assert not database.exists()


def test_a_graph_file_that_is_missing_or_wrong_is_one_sentence(tmp_path: Path, db: str) -> None:
    with pytest.raises(ServeRefused, match="cannot read the graph file"):
        prepare_serve(tmp_path / "none.json", db, "127.0.0.1:0")
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(ServeRefused, match="is not a graph file"):
        prepare_serve(tmp_path / "bad.json", db, "127.0.0.1:0")


def test_a_database_that_cannot_be_opened_is_one_sentence_and_frees_the_port(
    tmp_path: Path, sdlc: dict[str, Any]
) -> None:
    graph = write(tmp_path / "sdlc.json", sdlc)
    listen = free()

    with pytest.raises(ServeRefused, match="cannot open the database"):
        prepare_serve(graph, "nosuchdriver://x", listen)

    host, _, port = listen.rpartition(":")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as again:
        again.bind((host, int(port)))  # the refused control plane closed its port


def test_a_failure_after_the_store_opens_closes_the_store_and_the_listener(
    tmp_path: Path, sdlc: dict[str, Any], db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure anywhere between opening the store and returning `Serving` (here,
    building the URL) gives up the hold and frees the port."""
    graph = write(tmp_path / "sdlc.json", sdlc)
    listen = free()

    def broken(host: str, listener: socket.socket) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(serving_module, "_url", broken)

    with pytest.raises(RuntimeError, match="boom"):
        prepare_serve(graph, db, listen)

    Store(db, hold=True).close()  # the hold was given up
    host, _, port = listen.rpartition(":")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as again:
        again.bind((host, int(port)))  # the listener was closed


def test_a_keyboard_interrupt_while_opening_the_store_closes_the_listener(
    tmp_path: Path, sdlc: dict[str, Any], db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `KeyboardInterrupt` during `_open` (e.g. a slow migration) is not a
    `ServeRefused`, but it still frees the port."""
    graph = write(tmp_path / "sdlc.json", sdlc)
    listen = free()

    def interrupted(db: str) -> Store:
        raise KeyboardInterrupt

    monkeypatch.setattr(serving_module, "_open", interrupted)

    with pytest.raises(KeyboardInterrupt):
        prepare_serve(graph, db, listen)

    host, _, port = listen.rpartition(":")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as again:
        again.bind((host, int(port)))  # the listener was closed


def test_a_wide_listen_prints_one_warning_line(
    tmp_path: Path,
    sdlc: dict[str, Any],
    db: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def loopback(host: str, port: int) -> socket.socket:
        """Bind 127.0.0.1 in place of the wide host: a test opens no port to the network."""
        return socket.create_server(("127.0.0.1", port))

    monkeypatch.setattr(serving_module, "bound", loopback)

    serving = prepare_serve(write(tmp_path / "sdlc.json", sdlc), db, "0.0.0.0:0")
    serving.close()

    err = capsys.readouterr().err
    assert err.startswith("warning: ")
    assert "no authentication" in err
    assert len(err.strip().splitlines()) == 1
    assert serving.url.startswith("http://127.0.0.1:")  # where this computer reaches it


def test_a_graph_file_that_is_not_utf8_is_one_sentence(tmp_path: Path, db: str) -> None:
    graph = tmp_path / "utf16.json"
    graph.write_bytes(b"\xff\xfe{\x00}\x00")

    with pytest.raises(ServeRefused, match="is not a graph file") as caught:
        prepare_serve(graph, db, "127.0.0.1:0")

    assert "\n" not in str(caught.value)


def test_a_graph_file_that_the_graph_cannot_read_is_one_sentence(
    tmp_path: Path, sdlc: dict[str, Any], db: str
) -> None:
    document = copy.deepcopy(sdlc)
    document["transitions"] = "x"
    graph = write(tmp_path / "odd.json", document)

    with pytest.raises(ServeRefused, match="is not a graph file") as caught:
        prepare_serve(graph, db, "127.0.0.1:0")

    assert "\n" not in str(caught.value)


def take_the_hold(db: str) -> None:
    """Write another control plane into the hold, as if it took the database."""
    engine = create_engine(db)
    with engine.begin() as connection:
        connection.execute(update(tables.ControlPlane).values(serving="another-control-plane"))
    engine.dispose()


def test_a_lost_hold_calls_on_lost_and_the_server_stops(
    tmp_path: Path, sdlc: dict[str, Any], db: str
) -> None:
    serving = prepare_serve(write(tmp_path / "sdlc.json", sdlc), db, "127.0.0.1:0")
    server = GracefulServer(serving.config)
    calls: list[None] = []

    def stop() -> None:
        calls.append(None)
        server.should_exit = True

    serving.on_lost(stop)
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [serving.listener]}, daemon=True
    )
    thread.start()
    try:
        until(lambda: server.started or not thread.is_alive())
        assert server.started, "the control plane did not start"
        take_the_hold(db)
        thread.join(timeout=15)
        assert not thread.is_alive(), "the server did not stop after the lost hold"
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        serving.close()

    assert calls == [None]
    assert serving.clock.lost
