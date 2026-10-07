"""The API, the client and `stateous token` with child tokens (design section 8)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from stateous import Client
from stateous.cli.main import app
from stateous.client import Refused
from stateous.control.engine import Engine
from stateous.control.graph import Graph
from stateous.control.store import Store

from ..parallel import DESIGN, SECURITY, TOP, parallel_document


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In place of the `engine` of conftest.py, so that `http` and `served` serve the
    workflow with a parallel state. T-1 is in `reviews`."""
    store = Store("sqlite://")
    made = Engine(store, Graph(parallel_document()))
    made.start(TOP, {"repo": "acme/api"})
    made.fire(TOP, "done", None)
    yield made
    store.close()


def test_a_child_shows_its_parent_and_a_top_token_has_none(http: TestClient) -> None:
    child = http.get(f"/tokens/{SECURITY}").json()
    top = http.get(f"/tokens/{TOP}").json()

    assert (child["parent"], child["state"]) == (TOP, "triage")
    assert "parent" not in top


def test_the_children_of_a_token_are_listed_with_the_parent_query(http: TestClient) -> None:
    listed = http.get("/tokens", params={"parent": TOP})

    assert listed.status_code == 200
    assert sorted(token["token"] for token in listed.json()) == [DESIGN, SECURITY]
    assert http.get("/tokens", params={"parent": SECURITY}).json() == []
    assert len(http.get("/tokens").json()) == 3  # children are tokens too


def test_a_parent_query_that_is_too_long_is_422(http: TestClient) -> None:
    answer = http.get("/tokens", params={"parent": "x" * 129})

    assert answer.status_code == 422
    assert answer.json() == {"error": "'parent' is a token id of 1 to 128 characters"}


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
def test_a_child_cannot_be_steered_alone_409(http: TestClient, action: str) -> None:
    answer = http.post(f"/tokens/{SECURITY}/{action}")

    assert answer.status_code == 409
    error = answer.json()["error"]
    assert "it is part of its parent's work" in error
    assert error.endswith(f"{action} 'T-1' instead")


def test_a_supervisor_claims_a_child_state_by_its_claim_name(http: TestClient) -> None:
    answer = http.post("/tasks/claim", json={"supervisor": "box", "states": ["security/triage"]})

    assert answer.status_code == 200
    assert (answer.json()["token"], answer.json()["state"]) == (SECURITY, "security/triage")


def test_a_claim_takes_state_names_of_up_to_129_characters(http: TestClient) -> None:
    longest = "w" * 64 + "/" + "s" * 64

    fits = http.post("/tasks/claim", json={"supervisor": "box", "states": [longest]})
    too_long = http.post("/tasks/claim", json={"supervisor": "box", "states": [longest + "s"]})

    assert (fits.status_code, too_long.status_code) == (204, 422)


def test_the_client_lists_the_children_and_is_refused_a_child(served: str) -> None:
    with Client(served) as client:
        children = client.tokens(parent=TOP)
        with pytest.raises(Refused) as refused:
            client.cancel(SECURITY)

    assert sorted(child["token"] for child in children) == [DESIGN, SECURITY]
    assert refused.value.status == 409


def test_show_lists_the_children_under_the_parent(served: str) -> None:
    result = CliRunner().invoke(app, ["token", "show", TOP, "--url", served])

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "T-1: reviews, active"
    assert sorted(lines[1:3]) == [
        f"  child {DESIGN}: triage, active",
        f"  child {SECURITY}: triage, active",
    ]
    assert any(line.endswith(f"started the child {SECURITY}") for line in lines)
