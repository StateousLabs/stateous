"""`on_task_ready`: the control plane says when a task can be claimed."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from stateous import ControlPlane, InvalidContext
from stateous.model.wire import Outcome

from ...parallel import TOP, parallel_document
from .support import TICKET, sdlc, sqlite_url


@pytest.fixture
def plane(tmp_path: Path) -> Iterator[ControlPlane]:
    with ControlPlane(sdlc(), database=sqlite_url(tmp_path / "r.db"), migrate=True) as plane:
        yield plane


def heard(plane: ControlPlane) -> list[str]:
    states: list[str] = []
    plane.on_task_ready(lambda state, labels: states.append(state))
    return states


def test_ready_is_called_when_a_task_is_queued(plane: ControlPlane) -> None:
    states = heard(plane)

    plane.start("T-1", TICKET)

    assert states == ["coding"]


def test_ready_is_called_on_a_retry(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET)
    states = heard(plane)
    grant = plane.claim("box-1", ["coding"])
    assert grant is not None

    plane.report(grant.task, Outcome(kind="failed", error="flaky"), supervisor="box-1")

    assert states == ["coding"]


def test_ready_is_called_on_resume(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET)
    plane.pause("T-1")
    states = heard(plane)

    plane.resume("T-1")

    assert states == ["coding"]


def test_ready_is_not_called_for_a_rolled_back_change(plane: ControlPlane) -> None:
    states = heard(plane)

    with pytest.raises(InvalidContext):
        plane.start("T-1", {"repo": 7})

    assert states == []


def test_a_child_task_is_named_as_a_claim_names_it(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "p.db")
    with ControlPlane(parallel_document("all"), database=url, migrate=True) as plane:
        plane.start(TOP, {"repo": "acme/api"})
        states = heard(plane)

        plane.fire(TOP, "done", None)

        assert sorted(states) == ["design/triage", "security/triage"]


def test_the_returned_function_removes_the_callback(plane: ControlPlane) -> None:
    states: list[str] = []
    remove = plane.on_task_ready(lambda state, labels: states.append(state))
    remove()

    plane.start("T-1", TICKET)

    assert states == []


def test_a_failing_ready_callback_does_not_fail_the_call(
    plane: ControlPlane, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(state: str, labels: object) -> None:
        raise RuntimeError("the queue is down")

    plane.on_task_ready(broken)
    caplog.set_level(logging.ERROR)

    plane.start("T-1", TICKET)

    assert plane.get("T-1").state == "coding"
    assert "the queue is down" in caplog.text
