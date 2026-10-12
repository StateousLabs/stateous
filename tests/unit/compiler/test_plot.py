"""The workflow as a Mermaid state diagram: a pure function of the graph document."""

from typing import Any

from pydantic import BaseModel

from stateous import END, START, Workflow
from stateous.compiler.compile import compile_workflow
from stateous.compiler.plot import mermaid, state_id

from ...parallel import ALL_APPROVED, parallel_document


def test_it_is_a_mermaid_state_diagram_from_start_to_end(sdlc_document: dict[str, Any]) -> None:
    picture = mermaid(sdlc_document)

    assert picture.startswith("stateDiagram-v2\n    direction LR\n")
    assert "    [*] --> coding\n" in picture
    assert "    testing --> [*]: passed\n" in picture
    assert picture.endswith("\n")


def test_edges_carry_their_event_and_guard(sdlc_document: dict[str, Any]) -> None:
    picture = mermaid(sdlc_document)

    assert "    coding --> testing: done\n" in picture
    assert "    testing --> coding: failed [count('failed') < 3]\n" in picture
    assert "    testing --> [*]: failed [otherwise]\n" in picture


def test_start_and_end_are_never_drawn_as_states(sdlc_document: dict[str, Any]) -> None:
    picture = mermaid(sdlc_document)

    assert "__start__" not in picture
    assert "__end__" not in picture
    assert "__started__" not in picture


class Ticket(BaseModel):
    priority: str = "low"


def odd() -> dict[str, Any]:
    flow = Workflow("sdlc", context=Ticket)
    urgent, queued = flow.state("urgent", timeout="1h"), flow.state("in-queue", retries=1)
    flow.transition(START, urgent, when="ctx['priority'] == 'high'")
    flow.transition(START, queued, otherwise=True)
    flow.transition(queued, urgent, on=flow.event("picked"))
    flow.transition(queued, END, on="error")
    flow.transition(urgent, END, on="timeout")
    return compile_workflow(flow)


def test_starts_transitions_are_drawn_with_their_guards_only() -> None:
    picture = mermaid(odd())

    assert "    [*] --> urgent: [ctx['priority'] == 'high']\n" in picture
    assert "    [*] --> in_queue: [otherwise]\n" in picture


def test_stateous_own_events_are_named_plainly() -> None:
    picture = mermaid(odd())

    assert "    urgent --> [*]: (timeout)\n" in picture
    assert "    in_queue --> [*]: (error)\n" in picture


def test_states_whose_names_are_not_identifiers_keep_their_names() -> None:
    assert '    state "in-queue" as in_queue\n' in mermaid(odd())


def test_a_tokens_current_state_can_be_lit(sdlc_document: dict[str, Any]) -> None:
    picture = mermaid(sdlc_document, current="testing")

    assert "classDef current" in picture
    assert "    class testing current\n" in picture
    assert "classDef current" not in mermaid(sdlc_document)


def test_a_finished_tokens_last_state_is_drawn_as_finished(
    sdlc_document: dict[str, Any],
) -> None:
    picture = mermaid(sdlc_document, current="testing", finished=True)

    assert "classDef finished" in picture
    assert "    class testing finished\n" in picture
    assert "current" not in picture


def test_state_id_turns_hyphens_into_underscores() -> None:
    assert state_id("awaiting-review") == "awaiting_review"


# ── parallel states ────────────────────────────────────


def test_a_parallel_state_holds_its_workflows_as_regions() -> None:
    picture = mermaid(parallel_document())

    assert "    state reviews {\n" in picture
    assert '        state "security" as reviews__security {\n' in picture
    assert '            state "triage" as reviews__security__triage\n' in picture
    assert "            [*] --> reviews__security__triage\n" in picture
    assert (
        "            reviews__security__triage --> reviews__security__approved: lgtm\n"
    ) in picture
    region_break = picture.index("        --\n", picture.index("    state reviews {"))
    assert picture.index("reviews__security") < region_break < picture.index("reviews__design")


def test_joined_is_named_plainly() -> None:
    picture = mermaid(parallel_document())

    assert f"    reviews --> merging: (joined) [{ALL_APPROVED}]\n" in picture
    assert "    reviews --> coding: (joined) [otherwise]\n" in picture
    assert "    reviews --> blocked: (timeout)\n" in picture
