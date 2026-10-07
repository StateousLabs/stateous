"""The graph file: JSON text, its checksum and its structure (contract section 6)."""

import copy
import hashlib
import json
from typing import Any

import pytest

from stateous.model.graph_file import GraphFileError, checksum, dump_graph, load_graph, structure

from ...parallel import parallel_document


def test_the_file_is_indented_json_with_a_last_newline(sdlc_document: dict[str, Any]) -> None:
    text = dump_graph(sdlc_document)

    assert text == json.dumps(sdlc_document, indent=2, sort_keys=False) + "\n"
    assert text.startswith('{\n  "stateous": 1,\n  "workflow": "sdlc",')


def test_a_graph_file_round_trips(sdlc_document: dict[str, Any]) -> None:
    assert load_graph(dump_graph(sdlc_document)) == sdlc_document


def test_the_checksum_is_the_sha256_of_the_canonical_json(sdlc_document: dict[str, Any]) -> None:
    canonical = json.dumps(sdlc_document, sort_keys=True, separators=(",", ":"))

    assert checksum(sdlc_document) == "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def test_the_checksum_is_stable_and_changes_with_the_graph(sdlc_document: dict[str, Any]) -> None:
    changed = copy.deepcopy(sdlc_document)
    changed["transitions"][1]["to"] = "coding"

    assert checksum(sdlc_document) == checksum(copy.deepcopy(sdlc_document))
    assert checksum(changed) != checksum(sdlc_document)


@pytest.mark.parametrize(("key", "value"), [("timeout", "2h"), ("retries", 5)])
def test_structure_ignores_timeouts_and_retries(
    sdlc_document: dict[str, Any], key: str, value: object
) -> None:
    changed = copy.deepcopy(sdlc_document)
    changed["states"]["testing"][key] = value
    del changed["states"]["coding"][key]

    assert structure(changed) == structure(sdlc_document)
    assert checksum(changed) != checksum(sdlc_document)
    assert structure(sdlc_document).startswith("sha256:")


@pytest.mark.parametrize("where", ["guard", "start", "schema"])
def test_structure_changes_with_anything_else(sdlc_document: dict[str, Any], where: str) -> None:
    changed = copy.deepcopy(sdlc_document)
    if where == "guard":
        changed["transitions"][4]["when"] = "count('failed') < 5"
    elif where == "start":
        changed["transitions"][0]["to"] = "testing"
    else:
        changed["schemas"]["Ticket"]["properties"]["title"] = {"type": "string"}

    assert structure(changed) != structure(sdlc_document)


def test_structure_does_not_change_the_document(sdlc_document: dict[str, Any]) -> None:
    before = copy.deepcopy(sdlc_document)

    structure(sdlc_document)

    assert sdlc_document == before


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("{not json", "the graph file is not valid JSON"),
        ("[1, 2]", "the graph file is not a JSON object"),
        ('{"workflow": "x"}', "the graph file has no 'stateous'"),
    ],
)
def test_a_file_that_is_not_a_graph_is_refused(text: str, reason: str) -> None:
    with pytest.raises(GraphFileError, match=reason):
        load_graph(text)


@pytest.mark.parametrize("version", [2, True, "1", None])
def test_a_graph_of_another_format_is_refused(
    sdlc_document: dict[str, Any], version: object
) -> None:
    sdlc_document["stateous"] = version

    with pytest.raises(GraphFileError, match="this Stateous reads format 1"):
        load_graph(json.dumps(sdlc_document))


@pytest.mark.parametrize("key", ["workflow", "states", "events", "transitions", "schemas"])
def test_a_graph_without_a_key_is_refused(sdlc_document: dict[str, Any], key: str) -> None:
    del sdlc_document[key]

    with pytest.raises(GraphFileError, match=f"the graph file has no {key!r}"):
        load_graph(json.dumps(sdlc_document))


def test_states_must_be_an_object(sdlc_document: dict[str, Any]) -> None:
    sdlc_document["states"] = ["coding"]

    with pytest.raises(GraphFileError, match="'states' is not an object"):
        load_graph(json.dumps(sdlc_document))


def test_a_bad_timeout_is_refused_by_state(sdlc_document: dict[str, Any]) -> None:
    sdlc_document["states"]["testing"]["timeout"] = "3000000d"

    with pytest.raises(GraphFileError, match=r"state 'testing'.*too long"):
        load_graph(json.dumps(sdlc_document))


@pytest.mark.parametrize("retries", [-1, 1.5, True, "2", None])
def test_bad_retries_are_refused_by_state(sdlc_document: dict[str, Any], retries: Any) -> None:
    sdlc_document["states"]["testing"]["retries"] = retries

    with pytest.raises(GraphFileError, match=r"state 'testing': retries is a whole number"):
        load_graph(json.dumps(sdlc_document))


def test_retries_of_zero_or_more_are_read(sdlc_document: dict[str, Any]) -> None:
    sdlc_document["states"]["testing"]["retries"] = 0

    assert load_graph(json.dumps(sdlc_document))["states"]["testing"]["retries"] == 0


# ── child workflows (design section 8) ────────────────────────────────────


def test_a_graph_file_with_child_workflows_round_trips() -> None:
    document = parallel_document()

    assert load_graph(dump_graph(document)) == document


def test_an_overlong_timeout_is_refused_in_a_child_workflow() -> None:
    document = parallel_document()
    document["workflows"]["design"]["states"]["triage"]["timeout"] = "3000000d"

    with pytest.raises(GraphFileError, match=r"the graph file's state 'triage': "):
        load_graph(dump_graph(document))


def test_a_parallel_state_must_name_workflows_the_file_has() -> None:
    document = parallel_document()
    document["states"]["reviews"]["parallel"] = ["security", "legal"]

    with pytest.raises(GraphFileError, match="'parallel' names a workflow that 'workflows'"):
        load_graph(dump_graph(document))


def test_a_join_is_all_or_any_in_the_file_too() -> None:
    document = parallel_document()
    document["states"]["reviews"]["join"] = "most"

    with pytest.raises(GraphFileError, match="'join' is 'all' or 'any'"):
        load_graph(dump_graph(document))


def test_structure_ignores_the_timeouts_of_child_workflows_too() -> None:
    document = parallel_document()
    changed = copy.deepcopy(document)
    changed["workflows"]["design"]["states"]["triage"]["timeout"] = "1h"
    moved = copy.deepcopy(document)
    moved["workflows"]["design"]["transitions"][1]["to"] = "rejected"

    assert structure(changed) == structure(document)
    assert structure(moved) != structure(document)
