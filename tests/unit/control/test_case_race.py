"""Two token ids that differ only in case, made at the same time: the database
refuses the second. The engine checks first, but two calls under different family
locks (a top token's start, and a child's entry) can both pass that check. These
tests switch the check off to play that race on one database."""

from __future__ import annotations

import pytest

from stateous.control.engine import Engine, InvalidTokenId
from stateous.control.graph import Graph
from stateous.control.store import Store, Transaction

from ...parallel import SECURITY, TOP, parallel_document
from .support import Clock, new_engine

RACE = "differs only in case from one that another call made at the same time"


def nothing_found(self: Transaction, token: str) -> None:
    return None


def not_taken(self: Transaction, token: str) -> bool:
    return False


@pytest.fixture
def no_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine's own check finds nothing: as when another call has not yet committed."""
    monkeypatch.setattr(Transaction, "token_folded", nothing_found)
    monkeypatch.setattr(Transaction, "token_taken", not_taken)


@pytest.mark.usefixtures("no_check")
def test_a_start_that_loses_the_race_is_refused_as_an_invalid_token_id() -> None:
    engine = new_engine()
    engine.start("T-1", {"repo": "acme/api"})

    with pytest.raises(InvalidTokenId, match=RACE):
        engine.start("t-1", {"repo": "acme/api"})

    assert [token.token for token in engine.tokens()] == ["T-1"]


@pytest.mark.usefixtures("no_check")
def test_an_entry_whose_child_loses_the_race_is_refused_as_an_invalid_token_id() -> None:
    engine = Engine(Store("sqlite://"), Graph(parallel_document()), clock=Clock())
    engine.start(SECURITY.upper(), {"repo": "acme/api"})
    engine.start(TOP, {"repo": "acme/api"})

    with pytest.raises(InvalidTokenId, match=RACE):
        engine.fire(TOP, "done", None)

    assert engine.get(TOP).state == "coding"  # nothing of the entry was kept
    assert engine.tokens(parent=TOP) == []
