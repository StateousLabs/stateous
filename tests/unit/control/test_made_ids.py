"""Ids that Stateous makes: time-ordered, and safe to retry with an idempotency key."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from stateous import ControlPlane, InvalidArgument, InvalidPayload
from stateous.control.ids import new_id

from .support import TICKET, sdlc, sqlite_url

ULID = re.compile(r"[0-9a-hjkmnp-tv-z]{26}")


@pytest.fixture
def plane(tmp_path: Path) -> Iterator[ControlPlane]:
    with ControlPlane(sdlc(), database=sqlite_url(tmp_path / "m.db"), migrate=True) as plane:
        yield plane


def test_made_ids_are_ulids_in_order() -> None:
    made = [new_id() for _ in range(500)]

    assert all(ULID.fullmatch(made_id) for made_id in made)
    assert made == sorted(made)
    assert len(set(made)) == 500


def test_a_start_with_no_id_gets_a_made_id_in_order(plane: ControlPlane) -> None:
    first = plane.start(None, TICKET)
    second = plane.start(None, TICKET)

    assert ULID.fullmatch(first.token) and ULID.fullmatch(second.token)
    assert first.token < second.token
    assert plane.get(first.token).token == first.token


def test_a_retried_start_with_a_key_gives_one_token(plane: ControlPlane) -> None:
    first = plane.start(None, TICKET, key="order-981")
    again = plane.start(None, TICKET, key="order-981")

    assert again.token == first.token
    assert len(plane.tokens()) == 1
    assert plane.known_start_key("order-981")
    assert not plane.known_start_key("order-982")


def test_a_key_in_two_scopes_gives_two_tokens(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "s.db")
    with ControlPlane(sdlc(), database=url, migrate=True, scope=["user"]) as plane:
        a = plane.start(None, TICKET, labels={"user": "a"}, key="k")
        b = plane.start(None, TICKET, labels={"user": "b"}, key="k")
        again = plane.start(None, TICKET, labels={"user": "a"}, key="k")

        assert a.token != b.token
        assert again.token == a.token


def test_an_id_and_a_key_together_are_refused(plane: ControlPlane) -> None:
    with pytest.raises(InvalidArgument, match="not both"):
        plane.start("T-1", TICKET, key="k")


def test_a_bad_key_is_refused(plane: ControlPlane) -> None:
    for key in ("", "x" * 257, "a\nb"):
        with pytest.raises(InvalidPayload):
            plane.start(None, TICKET, key=key)
