"""Two guards that could both be true are refused when compiling (design, section 5.1)."""

from typing import Literal

import pytest
from pydantic import BaseModel

from stateous import END, START, Workflow
from stateous.compiler.validate import validate
from stateous.model.workflow import DefinitionError


class Ticket(BaseModel):
    a: int | None = None
    b: int | None = None
    brief: str | None = None


class Verdict(BaseModel):
    score: int
    severity: Literal["high", "low"]
    ok: bool


def checked(*guards: str, otherwise: bool = False) -> None:
    """A workflow where `judge` leaves on `verdict` along one transition per guard."""
    flow = Workflow("sdlc", context=Ticket)
    judge = flow.state("judge")
    verdict = flow.event("verdict", payload=Verdict)
    flow.transition(START, judge)
    for guard in guards:
        flow.transition(judge, END, on=verdict, when=guard)
    if otherwise:
        flow.transition(judge, END, on=verdict, otherwise=True)
    validate(flow)


def refused(*guards: str) -> str:
    with pytest.raises(DefinitionError) as caught:
        checked(*guards)
    return str(caught.value)


# ── never both true: compiles ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "guards",
    [
        ("payload.score > 5", "payload.score <= 5"),
        ("payload.score > 5", "not (payload.score > 5)"),
        ("payload.severity == 'high'", "payload.severity == 'low'"),
        ("payload.ok == true", "payload.ok == false"),
        ("ctx['brief'] == null", "ctx['brief'] != null"),
        ("count('verdict') < 3", "count('verdict') >= 3"),
        ("payload.score < 0 or payload.score > 10", "payload.score >= 0 and payload.score <= 10"),
        ("payload.score in [1, 2]", "payload.score in [3, 4]"),
    ],
)
def test_guards_that_can_never_both_be_true_compile(guards: tuple[str, str]) -> None:
    checked(*guards)


def test_one_guard_and_a_fallback_always_compiles() -> None:
    checked("payload.score > 5", otherwise=True)


def test_the_payloads_types_are_used_to_prove_it() -> None:
    # Any other string would satisfy both, but severity is only 'high' or 'low'.
    checked("payload.severity != 'high'", "payload.severity != 'low'")


# ── both can be true: refused, with an example ────────────────────────────


def test_guards_that_can_both_be_true_are_refused_with_an_example() -> None:
    problem = refused("payload.score > 5", "payload.score > 3")

    assert "'verdict' from 'judge'" in problem
    assert "'payload.score > 5' and 'payload.score > 3' can both be true" in problem
    assert "payload.score = 6" in problem


def test_guards_on_different_fields_overlap_when_both_hold() -> None:
    problem = refused("ctx['a'] == 1", "ctx['b'] == 2")

    assert "ctx['a'] = 1" in problem and "ctx['b'] = 2" in problem


def test_an_example_respects_what_counts_can_be() -> None:
    # The event being routed is counted already, so its count is at least 1.
    problem = refused("count('verdict') < 3", "count('verdict') > 1")

    assert "count('verdict') = 2" in problem


def test_every_overlapping_pair_is_reported() -> None:
    problem = refused("payload.score > 5", "payload.score > 3", "payload.score > 1")

    assert problem.count("can both be true") == 3


def test_a_literal_1_is_not_mistaken_for_true() -> None:
    problem = refused("ctx['a'] == true or ctx['a'] == 1", "ctx['a'] == 1")

    assert "ctx['a'] = 1" in problem


# ── cannot be decided: refused ────────────────────────────────────────────


def test_comparing_two_values_cannot_be_proven_and_is_refused() -> None:
    problem = refused("payload.score > ctx['a']", "payload.score < 3")

    assert "cannot prove" in problem
    assert "a guard compares two values with each other" in problem
    assert "otherwise=True" in problem


def test_searching_a_value_that_is_not_a_list_cannot_be_proven() -> None:
    problem = refused("1 in payload.score", "payload.score > 3")

    assert "`in` searches payload.score, which is not a list" in problem
