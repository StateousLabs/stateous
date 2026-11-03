"""Keys inside, public ids outside: the engine works on `<digest>:<id>`, and nothing that
leaves it shows the digest."""

from __future__ import annotations

import json
from typing import Any

import pytest

from stateous.control.engine import Engine, TokenNotActive, TokenNotFound
from stateous.control.graph import Graph
from stateous.control.scope import digest_of, key_of, public_id, scope_digest
from stateous.control.store import Store
from stateous.model.wire import Outcome

from ...parallel import parallel_document
from .support import Clock

DIGEST = scope_digest({"user": "u1"})
TOP = key_of(DIGEST, "T-1")


def test_keys_round_trip() -> None:
    assert len(DIGEST) == 16
    assert public_id(TOP) == "T-1"
    assert digest_of(TOP) == DIGEST
    assert public_id("T-1") == "T-1"
    assert digest_of("T-1") is None
    assert key_of(None, "T-1") == "T-1"
    assert scope_digest({"a": "1", "b": "2"}) == scope_digest({"b": "2", "a": "1"})


def no_digest(value: Any) -> None:
    text = json.dumps(value, default=str)
    assert DIGEST not in text, text


def test_no_key_leaves_stateous() -> None:
    store = Store("sqlite://")
    store.migrate(notices=True)
    engine = Engine(store, Graph(parallel_document("all")), clock=Clock())

    started = engine.start(TOP, {"repo": "acme/api"})
    engine.fire(TOP, "done", None)
    children = engine.tokens(parent=TOP)
    for child in children:
        grant = engine.claim("box", [f"{child.token.split('.')[-1]}/triage"])
        assert grant is not None
        no_digest(grant.to_json())
        engine.report(grant.task, Outcome(kind="event", event="lgtm"), supervisor="box")
    for child in children:
        engine.fire(key_of(DIGEST, child.token), "recorded", None)

    no_digest([started, children, engine.get(TOP)])
    no_digest([entry.data for entry in engine.history(TOP)])
    for child in children:
        no_digest([entry.data for entry in engine.history(key_of(DIGEST, child.token))])
    assert started.token == "T-1"
    assert all(child.parent == "T-1" for child in children)
    with store.transaction(engine.clock()) as tx:
        from sqlalchemy import select

        from stateous.control.store import tables

        session = tx._session  # pyright: ignore[reportPrivateUsage]
        notices = list(session.scalars(select(tables.Notice)))
    assert notices
    no_digest([(n.token, n.parent) for n in notices])


def test_messages_name_the_public_id() -> None:
    engine = Engine(Store("sqlite://"), Graph(parallel_document("all")), clock=Clock())
    engine.start(TOP, {"repo": "acme/api"})
    engine.cancel(TOP)

    with pytest.raises(TokenNotFound) as missing:
        engine.get(key_of(DIGEST, "NOPE"))
    with pytest.raises(TokenNotActive) as refused:
        engine.fire(TOP, "done", None)

    assert DIGEST not in str(missing.value) and "'NOPE'" in str(missing.value)
    assert DIGEST not in str(refused.value)


def test_the_id_limit_counts_the_public_id() -> None:
    engine = Engine(Store("sqlite://"), Graph(parallel_document("all")), clock=Clock())
    long_id = "T" * 100  # the key is 117 characters; its children's ids fit in 128

    engine.start(key_of(DIGEST, long_id), {"repo": "acme/api"})
    engine.fire(key_of(DIGEST, long_id), "done", None)

    assert len(engine.tokens(parent=key_of(DIGEST, long_id))) == 2
