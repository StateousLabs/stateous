"""The fixes of the final review: the try-out's own outbox, the watchdog, the sentence of
a contract for another graph, JSON from a file, the check of each fire, and a refused fire."""

from __future__ import annotations

import asyncio
import json
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest

from stateous import ControlPlane, EventNotAllowed
from stateous.tryout.main import main, start_watchdog
from stateous.tryout.run import check_fires, parse_fire, try_workflow

from .support import job

VERSION = version("stateous")


def test_the_try_out_leaves_the_folders_outbox_alone(tmp_path: Path) -> None:
    graph, folder = job(tmp_path, "return events.done()")
    report = asyncio.run(
        try_workflow(graph, folder, {"title": "a"}, timeout=30, database=tmp_path / "t.db")
    )
    assert report.stop.reason == "completed"
    assert not (folder / ".stateous").exists()


def test_the_watchdog_ends_a_try_out_that_hangs(capsys: pytest.CaptureFixture[str]) -> None:
    codes: list[int] = []
    timer = start_watchdog(0.05, Path("x.db"), exit=codes.append)
    timer.join(5)
    assert codes == [1]
    error = capsys.readouterr().err
    assert error.startswith("error: the try-out did not stop")
    assert "x.db" in error


def test_a_contract_for_another_graph_names_a_command_that_works(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    graph, folder = job(tmp_path, "return events.done()")
    other = json.loads(graph.read_text(encoding="utf-8"))
    other["states"]["work"]["timeout"] = "1h"
    other_graph = tmp_path / "other.json"
    other_graph.write_text(json.dumps(other), encoding="utf-8")
    argv = [str(other_graph), str(folder), "--context", '{"title": "a"}']
    code = main(argv, skill_version=VERSION)
    assert code == 1
    assert f"stateous init supervisor {other_graph} {folder}" in capsys.readouterr().err


def test_the_context_and_a_payload_can_come_from_a_file(tmp_path: Path) -> None:
    graph, folder = job(tmp_path, "return None")
    (tmp_path / "context.json").write_text('{"title": "a"}', encoding="utf-8")
    (tmp_path / "payload.json").write_text("{}", encoding="utf-8")
    argv = [str(graph), str(folder), "--context", f"@{tmp_path / 'context.json'}"]
    argv += ["--fire", f"work=done:@{tmp_path / 'payload.json'}"]
    assert main(argv, skill_version=VERSION) == 0


def test_a_file_that_is_not_there_is_wrong_use(tmp_path: Path) -> None:
    graph, folder = job(tmp_path, "return events.done()")
    argv = [str(graph), str(folder), "--context", f"@{tmp_path / 'no.json'}"]
    assert main(argv, skill_version=VERSION) == 2
    with pytest.raises(ValueError, match="STATE=EVENT"):
        parse_fire(f"work=done:@{tmp_path / 'no.json'}")


def test_a_fire_for_a_state_or_event_that_does_not_exist_is_wrong_use(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    graph, folder = job(tmp_path, "return events.done()")
    for fire in ("work=nope", "nowhere=done", "work=error"):
        argv = [str(graph), str(folder), "--context", '{"title": "a"}', "--fire", fire]
        assert main(argv, skill_version=VERSION) == 2, fire
    assert "nope" in capsys.readouterr().err


def test_check_fires_knows_child_states() -> None:
    from ...parallel import parallel_document

    document = parallel_document()
    check_fires(document, [parse_fire("security/triage=lgtm"), parse_fire("triage=deny")])
    with pytest.raises(ValueError, match="merged"):
        check_fires(document, [parse_fire("security/triage=merged")])


def test_a_fire_that_the_control_plane_refuses_keeps_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: ControlPlane, token: str, event: str, *args: Any, **kwargs: Any) -> Any:
        raise EventNotAllowed("no guard holds")

    monkeypatch.setattr(ControlPlane, "fire", refuse)
    graph, folder = job(tmp_path, "return None")
    report = asyncio.run(
        try_workflow(
            graph,
            folder,
            {"title": "a"},
            fires=[parse_fire("work=done")],
            timeout=30,
            database=tmp_path / "t.db",
        )
    )
    assert report.stop.reason == "fire_refused"
    assert "no guard holds" in report.stop.sentence
    assert report.steps
