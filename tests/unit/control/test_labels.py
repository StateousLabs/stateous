"""Labels on tokens: opaque string pairs, fixed at start, copied to child tokens."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from stateous import ControlPlane, InvalidArgument

from ...parallel import DESIGN, TOP, parallel_document
from .support import TICKET, sdlc, sqlite_url

U1 = {"user": "u1", "project": "p7"}


@pytest.fixture
def plane(tmp_path: Path) -> Iterator[ControlPlane]:
    with ControlPlane(sdlc(), database=sqlite_url(tmp_path / "l.db"), migrate=True) as plane:
        yield plane


def test_a_token_keeps_its_labels(plane: ControlPlane) -> None:
    started = plane.start("T-1", TICKET, labels=U1)

    assert started.labels == U1
    assert plane.get("T-1").labels == U1
    assert plane.start("T-2", TICKET).labels == {}


def test_starting_again_keeps_the_first_labels(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET, labels=U1)

    again = plane.start("T-1", TICKET, labels={"user": "someone-else"})

    assert again.labels == U1


def test_a_child_token_copies_its_parents_labels(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "p.db")
    with ControlPlane(parallel_document("all"), database=url, migrate=True) as plane:
        plane.start(TOP, {"repo": "acme/api"}, labels=U1)
        plane.fire(TOP, "done", None)

        assert plane.get(DESIGN).labels == U1


@pytest.mark.parametrize(
    "labels",
    [
        {f"k{n}": "v" for n in range(17)},
        {"Bad": "v"},
        {"user": ""},
        {"user": "x" * 257},
        {"user": 7},
        {"1user": "v"},
    ],
)
def test_bad_labels_are_refused(plane: ControlPlane, labels: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgument):
        plane.start("T-1", TICKET, labels=labels)


def test_good_labels_at_the_limits_are_kept(plane: ControlPlane) -> None:
    labels = {f"k.{n}-x_y": "v" * 256 for n in range(16)}

    assert plane.start("T-1", TICKET, labels=labels).labels == labels


def test_tokens_filter_by_all_given_labels(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET, labels=U1)
    plane.start("T-2", TICKET, labels={"user": "u1", "project": "p8"})
    plane.start("T-3", TICKET, labels={"user": "u2", "project": "p7"})

    both = plane.tokens(labels=U1)
    by_user = plane.tokens(labels={"user": "u1"})

    assert [t.token for t in both] == ["T-1"]
    assert sorted(t.token for t in by_user) == ["T-1", "T-2"]
    assert len(plane.tokens()) == 3
