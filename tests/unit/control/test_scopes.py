"""Token ids for each scope: one id in two scopes is two tokens, and a call never
reaches another scope."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from stateous import (
    ALL_SCOPES,
    ControlPlane,
    Forbidden,
    InvalidArgument,
    ScopeConflict,
    TaskNotFound,
    TokenNotFound,
)
from stateous.model.wire import Outcome

from .support import TICKET, sdlc, sqlite_url

U1 = {"user": "u1"}
U2 = {"user": "u2"}


def scope_of(caller: object) -> Mapping[str, str] | None:
    return None if caller is None else {"user": str(caller)}


@pytest.fixture
def plane(tmp_path: Path) -> Iterator[ControlPlane]:
    url = sqlite_url(tmp_path / "s.db")
    with ControlPlane(sdlc(), database=url, migrate=True, scope=["user"], scope_of=scope_of) as p:
        yield p


def test_two_scopes_keep_two_tokens_with_one_id(plane: ControlPlane) -> None:
    plane.start("T-1", {"repo": "a"}, labels=U1, caller="u1")
    plane.start("T-1", {"repo": "b"}, labels=U2, caller="u2")

    assert plane.get("T-1", caller="u1").context["repo"] == "a"
    assert plane.get("T-1", caller="u2").context["repo"] == "b"
    assert plane.get("T-1", scope=U2).token == "T-1"
    with pytest.raises(TokenNotFound):
        plane.get("T-1", caller="u3")


def test_a_start_into_another_scope_is_forbidden(plane: ControlPlane) -> None:
    with pytest.raises(Forbidden):
        plane.start("T-1", TICKET, labels=U2, caller="u1")

    assert plane.tokens(scope=ALL_SCOPES) == []


def test_a_start_without_the_scope_keys_is_refused(plane: ControlPlane) -> None:
    with pytest.raises(InvalidArgument, match="user"):
        plane.start("T-1", TICKET, labels={"project": "p7"}, caller="u1")


def test_a_call_with_no_scope_in_a_scoped_namespace_is_refused(plane: ControlPlane) -> None:
    with pytest.raises(InvalidArgument, match="give scope="):
        plane.get("T-1")


def test_a_task_of_another_scope_is_not_found(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET, labels=U1, caller="u1")
    grant = plane.claim("box", ["coding"], caller="u1")
    assert grant is not None

    with pytest.raises(TaskNotFound):
        plane.note(grant.task, "x", caller="u2")
    with pytest.raises(TaskNotFound):
        plane.report(grant.task, Outcome(kind="event", event="stuck"), caller="u2")
    assert plane.heartbeat("box", [grant.task], caller="u2") == [grant.task]
    assert plane.heartbeat("box", [grant.task], caller="u1") == []


def test_a_claim_takes_only_its_scope(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET, labels=U1, caller="u1")  # the older task

    assert plane.claim("box", ["coding"], caller="u2") is None
    grant = plane.claim("box", ["coding"], caller="u1")
    assert grant is not None and grant.token == "T-1"


def test_all_scopes_lists_and_claims_across_scopes(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET, labels=U1, caller="u1")
    plane.start("T-2", TICKET, labels=U2, caller="u2")

    listed = plane.tokens(scope=ALL_SCOPES)
    grant = plane.claim("pool", ["coding"], scope=ALL_SCOPES)

    assert sorted(t.token for t in listed) == ["T-1", "T-2"]
    assert grant is not None
    assert [t.token for t in plane.tokens(caller="u1")] == ["T-1"]


def test_all_scopes_needs_scope_for_an_id_in_two_scopes(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET, labels=U1, caller="u1")
    plane.start("T-1", TICKET, labels=U2, caller="u2")
    plane.start("T-9", TICKET, labels=U2, caller="u2")

    with pytest.raises(InvalidArgument, match="2 scopes"):
        plane.get("T-1", scope=ALL_SCOPES)
    assert plane.get("T-9", scope=ALL_SCOPES).labels == U2
    with pytest.raises(TokenNotFound):
        plane.get("T-404", scope=ALL_SCOPES)


def test_a_child_parent_filter_is_in_the_scope(tmp_path: Path) -> None:
    from ...parallel import TOP, parallel_document

    url = sqlite_url(tmp_path / "p.db")
    with ControlPlane(
        parallel_document("all"), database=url, migrate=True, scope=["user"], scope_of=scope_of
    ) as plane:
        plane.start(TOP, {"repo": "a"}, labels=U1, caller="u1")
        plane.fire(TOP, "done", None, caller="u1")

        children = plane.tokens(parent=TOP, caller="u1")

        assert len(children) == 2
        assert all(child.parent == TOP for child in children)
        assert plane.tokens(parent=TOP, caller="u2") == []


def test_a_scope_change_with_tokens_is_refused(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "c.db")
    with ControlPlane(sdlc(), database=url, migrate=True, scope=["user"]) as plane:
        plane.start("T-1", TICKET, labels=U1, scope=U1)
        plane.cancel("T-1", scope=U1)  # an ended token still counts

    with pytest.raises(ScopeConflict) as refused:
        ControlPlane(sdlc(), database=url, scope=["user", "project"])
    assert refused.value.code == "scope_conflict"
    with pytest.raises(ScopeConflict):
        ControlPlane(sdlc(), database=url)


def test_a_scope_change_on_an_empty_namespace_is_allowed(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "e.db")
    ControlPlane(sdlc(), database=url, migrate=True, scope=["user"]).close()

    with ControlPlane(sdlc(), database=url, scope=["tenant"]) as plane:
        plane.start("T-1", TICKET, labels={"tenant": "t"}, scope={"tenant": "t"})


def test_bad_scope_keys_are_refused(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "b.db")
    for scope in (["Bad"], ["a", "b", "c", "d", "e"]):
        with pytest.raises(ValueError):
            ControlPlane(sdlc(), database=url, migrate=True, scope=scope)


def test_no_scope_works_as_before(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "n.db")
    with ControlPlane(sdlc(), database=url, migrate=True) as plane:
        plane.start("T-1", TICKET, labels=U1)
        assert plane.get("T-1").labels == U1


def test_a_start_with_no_scope_in_a_scoped_namespace_is_refused(plane: ControlPlane) -> None:
    plane.start("T-1", {"repo": "secret"}, labels=U2, caller="u2")

    with pytest.raises(InvalidArgument, match="give scope="):
        plane.start("T-1", TICKET, labels=U2)  # scope_of(None) is None
    with pytest.raises(InvalidArgument, match="give scope="):
        plane.known_start_key("k", labels=U2)


def test_a_scope_of_without_a_scope_key_is_invalid_on_start(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "k.db")
    with (
        ControlPlane(
            sdlc(), database=url, migrate=True, scope=["user"], scope_of=lambda caller: {"x": "y"}
        ) as plane,
        pytest.raises(InvalidArgument, match="no value for user"),
    ):
        plane.start("T-1", TICKET, labels=U1, caller="c")


def test_all_scopes_finds_an_id_only_in_its_exact_case(plane: ControlPlane) -> None:
    plane.start("Z-1", TICKET, labels=U1, caller="u1")

    with pytest.raises(TokenNotFound):
        plane.get("z-1", scope=ALL_SCOPES)


def test_the_authorizer_sees_the_scope_in_a_claims_labels(tmp_path: Path) -> None:
    seen: list[dict[str, str]] = []

    def record(caller: object, action: object, labels: Mapping[str, str]) -> bool:
        if action == "claim":
            seen.append(dict(labels))
        return True

    url = sqlite_url(tmp_path / "a.db")
    with ControlPlane(
        sdlc(), database=url, migrate=True, scope=["user"], scope_of=scope_of, authorize=record
    ) as plane:
        plane.claim("box", ["coding"], caller="u1")

    assert seen == [U1]
