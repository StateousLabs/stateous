"""The public `ControlPlane`: one workflow over one database, as a library object."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import stateous
from stateous import ControlPlane, ControlPlaneError, GraphConflict
from stateous.control.graph import Graph
from stateous.model.wire import Outcome, Schedule

from .support import Clock, sdlc, sqlite_url

TICKET = {"repo": "acme/api"}


def other_structure() -> dict[str, Any]:
    document = sdlc()
    document["states"]["review"] = {"emits": ["approved"]}
    document["events"]["approved"] = {}
    document["transitions"].append({"from": "review", "to": "__end__", "on": "approved"})
    document["transitions"][3] = {"from": "coding", "to": "review", "on": "done"}
    return document


def test_a_control_plane_runs_a_token_to_the_end(tmp_path: Path) -> None:
    with ControlPlane(sdlc(), database=sqlite_url(tmp_path / "s.db"), migrate=True) as plane:
        plane.start("T-1", TICKET)
        coding = plane.claim("box-1", ["coding"])
        assert coding is not None
        plane.report(coding.task, Outcome(kind="event", event="done"), supervisor="box-1")
        testing = plane.claim("box-1", ["testing"])
        assert testing is not None
        tests = {"passed": 3, "failed": 0}
        plane.report(
            testing.task, Outcome(kind="event", event="passed", payload=tests), supervisor="box-1"
        )

        assert plane.get("T-1").status == "completed"
        assert plane.name == "sdlc"
        assert [entry.kind for entry in plane.history("T-1")][-1] == "completed"


def test_two_control_planes_in_one_process(tmp_path: Path) -> None:
    first = ControlPlane(sdlc(), database=sqlite_url(tmp_path / "a.db"), migrate=True)
    second = ControlPlane(other_structure(), database=sqlite_url(tmp_path / "b.db"), migrate=True)
    try:
        first.start("T-1", {"repo": "acme/a"})
        second.start("T-1", {"repo": "acme/b"})

        assert first.get("T-1").context["repo"] == "acme/a"
        assert second.get("T-1").context["repo"] == "acme/b"
    finally:
        first.close()
        second.close()


def test_a_control_plane_reads_a_graph_file(tmp_path: Path) -> None:
    path = tmp_path / "sdlc.json"
    path.write_text(json.dumps(sdlc()), encoding="utf-8")

    with ControlPlane(path, database=sqlite_url(tmp_path / "s.db"), migrate=True) as plane:
        assert plane.health()["workflow"] == "sdlc"


def test_a_different_structure_with_live_tokens_is_refused(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "s.db")
    with ControlPlane(sdlc(), database=url, migrate=True) as plane:
        plane.start("T-1", TICKET)

    with pytest.raises(GraphConflict, match="active or paused"):
        ControlPlane(other_structure(), database=url)


def test_a_different_structure_with_no_live_tokens_is_recorded(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "s.db")
    ControlPlane(sdlc(), database=url, migrate=True).close()

    with ControlPlane(other_structure(), database=url) as plane:
        assert plane.health()["checksum"] == Graph(other_structure()).checksum


def test_a_new_checksum_is_recorded(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "s.db")
    with ControlPlane(sdlc(), database=url, migrate=True) as plane:
        plane.start("T-1", TICKET)
    changed = sdlc()
    changed["states"]["coding"]["timeout"] = "2h"  # an operational setting only

    with ControlPlane(changed, database=url) as plane:
        assert plane.get("T-1").status == "active"
        assert plane.health()["checksum"] == Graph(changed).checksum


def test_tick_fires_a_due_timer(tmp_path: Path) -> None:
    clock = Clock()
    with ControlPlane(
        sdlc(), database=sqlite_url(tmp_path / "s.db"), migrate=True, now=clock
    ) as plane:
        plane.start("T-1", TICKET)
        grant = plane.claim("box-1", ["coding"])
        assert grant is not None
        waiting = Outcome(
            kind="waiting", schedules=(Schedule(event="stuck", payload=None, after="10m"),)
        )
        plane.report(grant.task, waiting, supervisor="box-1")
        clock.advance(minutes=11)

        assert plane.tick() >= 1
        assert plane.get("T-1").status == "completed"


def test_importing_stateous_does_not_import_sqlalchemy() -> None:
    probe = "import stateous, sys; print('sqlalchemy' in sys.modules)"
    found = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, encoding="utf-8", check=True
    )
    assert found.stdout.strip() == "False"


def test_the_errors_keep_their_codes() -> None:
    codes = {
        name: getattr(stateous, name).code
        for name in stateous.__all__
        if isinstance(getattr(stateous, name), type)
        and issubclass(getattr(stateous, name), ControlPlaneError)
    }

    assert codes["TokenNotFound"] == "token_not_found"
    assert codes["EventNotAllowed"] == "event_not_allowed"
    assert codes["GraphConflict"] == "graph_conflict"
    assert codes["HoldLost"] == "not_serving"
    assert codes["ControlPlaneError"] == "unexpected"
