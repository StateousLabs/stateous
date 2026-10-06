"""The graph: the control plane's view of the compiled workflow."""

from stateous.control.graph import END, STARTED, Graph
from stateous.model.graph_file import checksum, structure

from .support import sdlc


def test_a_token_starts_where_starts_transitions_lead() -> None:
    graph = Graph(sdlc())

    found = [(t.target, t.event, t.when is not None, t.otherwise) for t in graph.starts]

    assert found == [("triage", STARTED, True, False), ("coding", STARTED, False, True)]


def test_each_state_keeps_its_settings_and_its_events() -> None:
    graph = Graph(sdlc())

    coding, triage = graph.state("coding"), graph.state("triage")

    assert (coding.emits, coding.timeout, coding.retries) == (("done", "stuck"), "6h", 2)
    assert (triage.emits, triage.timeout, triage.retries) == (("triaged",), None, 0)


def test_candidates_are_the_transitions_for_an_event_from_a_state() -> None:
    graph = Graph(sdlc())

    assert [t.target for t in graph.candidates("testing", "failed")] == ["coding", END]
    assert [t.target for t in graph.candidates("coding", "timeout")] == ["triage"]
    assert graph.candidates("triage", "passed") == []


def test_only_the_events_of_the_workflow_are_declared() -> None:
    graph = Graph(sdlc())

    assert graph.declares("done")
    assert not graph.declares("timeout")
    assert not graph.declares("error")
    assert not graph.declares(STARTED)
    assert not graph.declares("dnoe")


def test_payloads_are_checked_against_the_schemas_in_the_document() -> None:
    graph = Graph(sdlc())

    assert graph.payload_errors("passed", {"passed": 3, "failed": 0}) == []
    errors = graph.payload_errors("passed", {"passed": "three", "failed": 0})
    assert len(errors) == 1
    assert errors[0].startswith("passed:")


def test_an_event_without_a_payload_takes_none() -> None:
    graph = Graph(sdlc())

    assert graph.payload_errors("done", None) == []
    assert graph.payload_errors("done", {}) == []
    assert graph.payload_errors("done", {"x": 1}) == ["'done' carries no payload"]
    assert graph.payload_errors("timeout", None) == []


def test_the_context_is_checked_and_its_defaults_are_filled_in() -> None:
    graph = Graph(sdlc())

    assert graph.context_errors({"repo": "acme/api"}) == []
    assert graph.context_errors({}) == ["Ticket: 'repo' is a required property"]
    assert graph.with_defaults({"repo": "acme/api"}) == {
        "repo": "acme/api",
        "rejected": 0,
        "urgent": False,
    }


def test_a_workflow_without_a_context_takes_any_context() -> None:
    document = sdlc()
    del document["context"]
    graph = Graph(document)

    assert graph.context_errors({"anything": 1}) == []
    assert graph.with_defaults({"a": 1}) == {"a": 1}


def test_the_graph_knows_its_document_checksum_and_structure() -> None:
    document = sdlc()

    graph = Graph(document)

    assert graph.document is document
    assert (graph.name, graph.checksum, graph.structure) == (
        "sdlc",
        checksum(document),
        structure(document),
    )
