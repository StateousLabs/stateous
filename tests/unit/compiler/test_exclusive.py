"""Two guards that could both be true are refused when compiling (design, section 5.1)."""

from enum import Enum
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

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


# ── enum fields, lists and negative literals ──────────────────────────────


class Sev(Enum):
    HIGH = "high"
    LOW = "low"


class Alarm(BaseModel):
    sev: Sev
    maybe: Sev | None = None


class Counter(BaseModel):
    n: int = 0
    tags: list[str] = []
    delta: int = 0
    meta: dict[str, int] = {}


def alarm_checked(*guards: str) -> None:
    """Like `checked`, but the payload has an Enum field and the context has a list."""
    flow = Workflow("sdlc", context=Counter)
    judge = flow.state("judge")
    alarm = flow.event("alarm", payload=Alarm)
    flow.transition(START, judge)
    for guard in guards:
        flow.transition(judge, END, on=alarm, when=guard)
    validate(flow)


def alarm_refused(*guards: str) -> str:
    with pytest.raises(DefinitionError) as caught:
        alarm_checked(*guards)
    return str(caught.value)


def test_two_equal_guards_on_an_enum_field_are_refused() -> None:
    problem = alarm_refused("payload.sev == 'high'", "payload.sev == 'high'")

    assert "payload.sev = 'high'" in problem


def test_an_optional_enum_field_is_read_through_its_definition() -> None:
    problem = alarm_refused("payload.maybe == 'low'", "payload.maybe != 'high'")

    assert "payload.maybe = 'low'" in problem


def test_the_values_of_an_enum_are_used_to_prove_it() -> None:
    alarm_checked("payload.sev != 'high'", "payload.sev != 'low'")


def test_a_number_read_as_true_or_false_is_tried_below_zero() -> None:
    problem = alarm_refused("ctx['n'] < 1", "ctx['n']")

    assert "ctx['n'] = -1" in problem


def test_a_list_literal_is_one_value_when_compared_for_equality() -> None:
    problem = alarm_refused("ctx['tags'] == ['a']", "ctx['tags'] == ['a']")

    assert "ctx['tags'] = ['a']" in problem


def test_a_list_that_holds_a_member_but_is_not_the_literal_is_tried() -> None:
    problem = alarm_refused("ctx['tags'] != ['a']", "'a' in ctx['tags']")

    assert "can both be true" in problem


def test_a_list_literal_searched_for_in_a_list_is_one_member() -> None:
    problem = alarm_refused("['a'] in ctx['tags']", "['a'] in ctx['tags']")

    assert "can both be true" in problem


def test_ordering_a_list_cannot_be_proven() -> None:
    problem = alarm_refused("ctx['tags'] < ['b']", "ctx['tags'] > ['a']")

    assert "cannot prove" in problem


def test_a_negative_literal_divides_the_numbers() -> None:
    alarm_checked("ctx['delta'] > -5", "ctx['delta'] <= -5")
    problem = alarm_refused("ctx['delta'] > -5", "ctx['delta'] < -3")

    assert "ctx['delta'] = -4" in problem


def test_a_negative_literal_in_a_list_is_tried() -> None:
    problem = alarm_refused("ctx['delta'] in [-1, -2]", "ctx['delta'] < 0")

    assert "ctx['delta'] = -2" in problem or "ctx['delta'] = -1" in problem


# ── empty collections, and models without a schema ───────────────────────


@pytest.mark.parametrize(
    "guards",
    [
        ("ctx['tags']", "'a' not in ctx['tags']"),
        ("ctx['tags']", "ctx['tags'] != ['a']"),
        ("not ctx['meta']", "ctx['meta'] != null"),
    ],
)
def test_a_list_or_dict_read_as_true_or_false_is_tried_empty_and_not(
    guards: tuple[str, str],
) -> None:
    problem = alarm_refused(*guards)

    assert "can both be true" in problem


def test_an_empty_and_a_not_empty_list_are_still_exclusive() -> None:
    alarm_checked("ctx['tags'] == []", "ctx['tags'] != []")


class Pipe:
    pass


class Piped(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    pipe: Pipe | None = None
    score: int = 0


def test_guards_on_a_model_without_a_json_schema_are_refused_not_crashed() -> None:
    flow = Workflow("sdlc")
    judge = flow.state("judge")
    piped = flow.event("piped", payload=Piped)
    flow.transition(START, judge)
    flow.transition(judge, END, on=piped, when="payload.score > 1")
    flow.transition(judge, END, on=piped, when="payload.score < 1")

    with pytest.raises(DefinitionError, match="the model Piped cannot be written as JSON Schema"):
        validate(flow)


# ── branches ───────────────────────────────────────────


def joined_checked(*guards: str) -> None:
    """A parallel state that leaves on `joined` along one transition per guard, and a
    fallback."""
    child = Workflow("review")
    waiting = child.state("waiting")
    child.transition(START, waiting)
    child.transition(waiting, END, on=child.event("lgtm"))
    flow = Workflow("sdlc", context=Ticket)
    reviews = flow.state("reviews", parallel=[child])
    flow.transition(START, reviews)
    for guard in guards:
        flow.transition(reviews, END, on="joined", when=guard)
    flow.transition(reviews, END, on="joined", otherwise=True)
    validate(flow)


def test_two_guards_with_branches_cannot_be_proven_and_are_refused() -> None:
    with pytest.raises(DefinitionError) as caught:
        joined_checked("branches.all(final == 'waiting')", "branches.any(final == 'x')")

    problem = str(caught.value)
    assert "cannot prove the guards" in problem
    assert "a guard with branches.all or branches.any cannot be proven exclusive" in problem


def test_a_guard_with_branches_beside_another_guard_is_refused() -> None:
    with pytest.raises(DefinitionError, match="cannot be proven exclusive"):
        joined_checked("branches.all(final == 'waiting') == true", "ctx['a'] == 1")


def test_one_guard_with_branches_and_a_fallback_is_valid() -> None:
    joined_checked("branches.all(status == 'completed' and final == 'waiting')")
