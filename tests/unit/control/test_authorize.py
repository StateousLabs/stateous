"""The authorizer: one developer function decides who may do what, by labels."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from stateous import ControlPlane, Forbidden
from stateous.control.access import Action
from stateous.model.wire import Outcome

from .support import TICKET, sdlc, sqlite_url

U1 = {"user": "u1"}
U2 = {"user": "u2"}


def only_own(caller: object, action: Action, labels: Mapping[str, str]) -> bool:
    """A caller may act only on its own user's tokens; the caller is the user's id.
    A caller of None (the service itself) may do all."""
    return caller is None or labels.get("user") == caller


@pytest.fixture
def plane(tmp_path: Path) -> Iterator[ControlPlane]:
    url = sqlite_url(tmp_path / "a.db")
    with ControlPlane(sdlc(), database=url, migrate=True, authorize=only_own) as plane:
        yield plane


def test_no_authorizer_allows_everything(tmp_path: Path) -> None:
    with ControlPlane(sdlc(), database=sqlite_url(tmp_path / "n.db"), migrate=True) as plane:
        plane.start("T-1", TICKET, labels=U1, caller="u2")
        assert plane.get("T-1", caller="anyone").labels == U1


def test_start_is_checked_with_its_labels(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET, labels=U1, caller="u1")

    with pytest.raises(Forbidden) as refused:
        plane.start("T-2", TICKET, labels=U1, caller="u2")

    assert refused.value.code == "forbidden"


def test_starting_a_token_of_another_tenant_again_is_forbidden(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET, labels=U1, caller="u1")

    with pytest.raises(Forbidden):
        plane.start("T-1", TICKET, labels=U2, caller="u2")


def test_a_claim_with_labels_the_caller_may_not_use_is_forbidden(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET, labels=U1)

    with pytest.raises(Forbidden):
        plane.claim("box-2", ["coding"], labels=U1, caller="u2")

    assert plane.claim("box-1", ["coding"], labels=U1, caller="u1") is not None


def test_a_report_is_checked_with_the_tasks_labels(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET, labels=U1)
    grant = plane.claim("box-1", ["coding"], labels=U1, caller="u1")
    assert grant is not None
    outcome = Outcome(kind="event", event="stuck")

    with pytest.raises(Forbidden):
        plane.report(grant.task, outcome, supervisor="box-1", caller="u2")
    with pytest.raises(Forbidden):
        plane.note(grant.task, "x", caller="u2")
    with pytest.raises(Forbidden):
        plane.set_memory(grant.task, "k", "v", caller="u2")

    plane.report(grant.task, outcome, supervisor="box-1", caller="u1")
    assert plane.get("T-1").status == "completed"


def test_an_unknown_task_is_still_not_found(plane: ControlPlane) -> None:
    from stateous import TaskNotFound

    with pytest.raises(TaskNotFound):
        plane.note(999, "x", caller="u2")


def test_a_refused_heartbeat_task_is_lost_and_not_renewed(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET, labels=U1)
    plane.start("T-2", TICKET, labels=U2)
    first = plane.claim("box", ["coding"], labels=U1)
    second = plane.claim("box", ["coding"], labels=U2)
    assert first is not None and second is not None

    lost = plane.heartbeat("box", [first.task, second.task], caller="u1")

    assert lost == [second.task]


def test_a_list_skips_refused_tokens_and_still_fills_the_limit(plane: ControlPlane) -> None:
    for n in range(10):
        plane.start(f"T-{n}", TICKET, labels=U1 if n % 2 else U2)

    found = plane.tokens(limit=3, caller="u2")

    assert len(found) == 3
    assert all(token.labels == U2 for token in found)


def test_reads_and_steering_are_checked_with_the_tokens_labels(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET, labels=U1)

    for call in (
        lambda: plane.get("T-1", caller="u2"),
        lambda: plane.history("T-1", caller="u2"),
        lambda: plane.pause("T-1", caller="u2"),
        lambda: plane.resume("T-1", caller="u2"),
        lambda: plane.cancel("T-1", caller="u2"),
        lambda: plane.fire("T-1", "stuck", None, caller="u2"),
    ):
        with pytest.raises(Forbidden):
            call()

    assert plane.get("T-1", caller="u1").status == "active"


def test_the_authorizer_gets_each_action_by_name(tmp_path: Path) -> None:
    seen: list[tuple[object, str]] = []

    def record(caller: object, action: Action, labels: Mapping[str, str]) -> bool:
        seen.append((caller, action))
        return True

    url = sqlite_url(tmp_path / "r.db")
    with ControlPlane(sdlc(), database=url, migrate=True, authorize=record) as plane:
        plane.start("T-1", TICKET, caller="c")
        plane.get("T-1", caller="c")
        grant = plane.claim("box", ["coding"], caller="c")
        assert grant is not None
        plane.heartbeat("box", [grant.task], caller="c")

    assert [action for _, action in seen] == ["start", "read", "claim", "heartbeat"]
    assert {caller for caller, _ in seen} == {"c"}
