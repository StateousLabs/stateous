"""Claims by labels, and `count_claimed` for entitlements."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from stateous import ControlPlane

from ...parallel import TOP, parallel_document
from .support import TICKET, sdlc, sqlite_url

U1 = {"user": "u1"}
U2 = {"user": "u2"}


@pytest.fixture
def plane(tmp_path: Path) -> Iterator[ControlPlane]:
    with ControlPlane(sdlc(), database=sqlite_url(tmp_path / "c.db"), migrate=True) as plane:
        yield plane


def test_a_claim_with_labels_takes_only_matching_tasks(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET, labels=U1)  # the older task
    plane.start("T-2", TICKET, labels=U2)

    grant = plane.claim("box-2", ["coding"], labels=U2)

    assert grant is not None and grant.token == "T-2"
    assert plane.claim("box-2", ["coding"], labels=U2) is None


def test_a_claim_without_labels_takes_any_task(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET, labels=U1)

    grant = plane.claim("box-1", ["coding"])

    assert grant is not None and grant.token == "T-1"


def test_a_child_task_is_claimed_with_the_parents_labels(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "p.db")
    with ControlPlane(parallel_document("all"), database=url, migrate=True) as plane:
        plane.start(TOP, {"repo": "acme/api"}, labels=U1)
        plane.fire(TOP, "done", None)

        grant = plane.claim("box-1", ["design/triage"], labels=U1)

        assert grant is not None
        assert plane.claim("box-1", ["security/triage"], labels=U2) is None


def test_count_claimed_counts_only_matching_claimed_tasks(plane: ControlPlane) -> None:
    for n, labels in enumerate([U1, U1, U2]):
        plane.start(f"T-{n}", TICKET, labels=labels)
    plane.claim("box-1", ["coding"], labels=U1)
    plane.claim("box-2", ["coding"], labels=U2)

    assert plane.count_claimed(U1) == 1
    assert plane.count_claimed(U2) == 1
    assert plane.count_claimed({"user": "u3"}) == 0


def test_a_repeated_claim_with_other_labels_does_not_get_the_grant(plane: ControlPlane) -> None:
    plane.start("A-1", TICKET, labels={"tenant": "a"})
    first = plane.claim("worker", ["coding"], request="r1", labels={"tenant": "a"})
    assert first is not None

    again = plane.claim("worker", [], request="r1", labels={"tenant": "b"})

    assert again is None
    same = plane.claim("worker", [], request="r1", labels={"tenant": "a"})
    assert same == first


def test_a_grant_carries_the_tokens_labels(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET, labels=U1)

    grant = plane.claim("box-1", ["coding"])

    assert grant is not None and grant.labels == U1
