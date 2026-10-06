"""`stateous serve`: its refusals as one sentence, and a real process that a signal
stops gracefully."""

import json
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx2
from sqlalchemy import create_engine, update
from typer.testing import CliRunner

from stateous import Client
from stateous.cli.main import app
from stateous.cli.serving import prepare_serve
from stateous.control.store import tables

runner = CliRunner()


def write(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def free() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def database(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'stateous.db').as_posix()}"


def test_serve_is_a_command() -> None:
    assert "serve" in runner.invoke(app, ["--help"]).output


def test_serve_refuses_a_port_in_use_with_one_sentence(
    tmp_path: Path, sdlc: dict[str, Any]
) -> None:
    graph = write(tmp_path / "sdlc.json", sdlc)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        result = runner.invoke(
            app,
            ["serve", str(graph), "--db", database(tmp_path), "--listen", f"127.0.0.1:{port}"],
        )

    assert result.exit_code == 1
    assert "already uses that port" in result.output
    assert "Traceback" not in result.output
    assert len(result.output.strip().splitlines()) == 1


def test_serve_refuses_a_listen_that_is_not_host_and_port(
    tmp_path: Path, sdlc: dict[str, Any]
) -> None:
    graph = write(tmp_path / "sdlc.json", sdlc)

    result = runner.invoke(
        app, ["serve", str(graph), "--db", database(tmp_path), "--listen", "0.0.0.0"]
    )

    assert result.exit_code == 1
    assert "is not HOST:PORT" in result.output
    assert not (tmp_path / "stateous.db").exists()


def test_serve_refuses_a_database_that_another_control_plane_serves(
    tmp_path: Path, sdlc: dict[str, Any]
) -> None:
    graph = write(tmp_path / "sdlc.json", sdlc)
    first = prepare_serve(graph, database(tmp_path), "127.0.0.1:0")
    try:
        result = runner.invoke(
            app,
            ["serve", str(graph), "--db", database(tmp_path), "--listen", f"127.0.0.1:{free()}"],
        )
    finally:
        first.close()

    assert result.exit_code == 1
    assert "serves this database" in result.output
    assert len(result.output.strip().splitlines()) == 1


def wait_for_health(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    with httpx2.Client(timeout=1) as http:
        while time.monotonic() < deadline:
            assert process.poll() is None, process.communicate()[1]
            try:
                if http.get(f"{url}/health").status_code == 200:
                    return
            except httpx2.TransportError:
                pass
            time.sleep(0.1)
    raise AssertionError("serve did not answer within 30 seconds")


def start_serve(tmp_path: Path, graph: Path, port: int) -> subprocess.Popen[str]:
    """`stateous serve` in its own process (on Windows: its own process group, so
    that CTRL_BREAK_EVENT reaches only it)."""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    command = [
        sys.executable, "-m", "stateous", "serve", str(graph),
        "--db", database(tmp_path), "--listen", f"127.0.0.1:{port}",
    ]  # fmt: skip
    return subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags
    )


STOP: int = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
"""The signal that stops `serve` gracefully."""


def test_serve_runs_until_a_signal_and_then_releases_the_database(
    tmp_path: Path, sdlc: dict[str, Any]
) -> None:
    graph = write(tmp_path / "sdlc.json", sdlc)
    port = free()
    url = f"http://127.0.0.1:{port}"
    process = start_serve(tmp_path, graph, port)
    try:
        wait_for_health(url, process)
        with Client(url) as client:
            assert client.start("T-1", {"repo": "acme/api"})["state"] == "coding"
        process.send_signal(STOP)
        _, err = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == 0, err
    assert f"serving on {url}" in err
    assert "stopped" in err
    assert "Traceback" not in err
    prepare_serve(graph, database(tmp_path), "127.0.0.1:0").close()  # the hold was given up


def test_serve_stops_with_exit_1_when_another_control_plane_takes_the_database(
    tmp_path: Path, sdlc: dict[str, Any]
) -> None:
    graph = write(tmp_path / "sdlc.json", sdlc)
    port = free()
    process = start_serve(tmp_path, graph, port)
    try:
        wait_for_health(f"http://127.0.0.1:{port}", process)
        engine = create_engine(database(tmp_path))
        with engine.begin() as connection:
            connection.execute(update(tables.ControlPlane).values(serving="another-control-plane"))
        engine.dispose()
        _, err = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.send_signal(STOP)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()

    assert process.returncode == 1, err
    assert "another control plane took this database" in err
    assert err.strip().splitlines()[-1].startswith("error: ")
    assert "Traceback" not in err
