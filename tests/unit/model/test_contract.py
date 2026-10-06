"""The types that a generated contract.py is built from (contract section 8)."""

import json
from typing import Any, assert_type

import jsonschema
import pytest
from pydantic import BaseModel

from stateous import Occurrence, OutcomeError, StateRef, on_state
from stateous.model.contract import implementation_of


class Report(BaseModel):
    passed: int
    failed: int


class passed(Occurrence[Report]):  # noqa: N801 — generated names mirror event names
    name = "passed"


class done(Occurrence[None]):  # noqa: N801
    name = "done"


class stuck(Occurrence[None]):  # noqa: N801
    name = "stuck"


testing = StateRef("testing", emits=(passed, done))


def fits_schema(schema: dict[str, Any], answer: Any) -> bool:
    try:
        jsonschema.validate(answer, schema, cls=jsonschema.Draft202012Validator)
    except jsonschema.ValidationError:
        return False
    return True


# ── occurrences ───────────────────────────────────────────────────────────


def test_an_occurrence_carries_its_event_name_and_payload() -> None:
    report = Report(passed=41, failed=1)

    happened = passed(report)

    assert (happened.name, happened.payload) == ("passed", report)
    assert passed.payload_model() is Report
    assert repr(happened) == "passed(Report(passed=41, failed=1))"


def test_an_occurrence_without_a_payload_takes_no_argument() -> None:
    assert done().payload is None
    assert done.payload_model() is None


# ── StateRef ──────────────────────────────────────────────────────────────


def test_a_state_ref_names_the_events_its_routine_may_emit() -> None:
    assert testing.may_emit(passed(Report(passed=0, failed=1)))
    assert testing.may_emit(done())
    assert not testing.may_emit(stuck())


def test_the_outcome_schema_has_one_branch_per_event() -> None:
    assert testing.outcome_schema() == {
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "event": {"const": "passed"},
                    "payload": Report.model_json_schema(),
                },
                "required": ["event", "payload"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"event": {"const": "done"}},
                "required": ["event"],
                "additionalProperties": False,
            },
        ]
    }


@pytest.mark.parametrize(
    ("answer", "fits"),
    [
        ({"event": "passed", "payload": {"passed": 3, "failed": 0}}, True),
        ({"event": "done"}, True),
        ({"event": "passed", "payload": {"passed": "x", "failed": 0}}, False),
        ({"event": "passed"}, False),
        ({"event": "done", "payload": {}}, False),
        ({"event": "stuck"}, False),
    ],
)
def test_the_outcome_schema_is_a_json_schema_that_checks_answers(
    answer: dict[str, Any], fits: bool
) -> None:
    assert fits_schema(testing.outcome_schema(), answer) is fits


class Failure(BaseModel):
    name: str


class Detailed(BaseModel):
    failures: list[Failure]


class detailed(Occurrence[Detailed]):  # noqa: N801
    name = "detailed"


def test_nested_models_are_inlined_so_no_reference_dangles() -> None:
    schema = StateRef("testing", emits=(detailed,)).outcome_schema()

    assert "$ref" not in json.dumps(schema)
    assert "$defs" not in json.dumps(schema)
    assert fits_schema(schema, {"event": "detailed", "payload": {"failures": [{"name": "a"}]}})
    assert not fits_schema(schema, {"event": "detailed", "payload": {"failures": [{"name": 1}]}})


class Node(BaseModel):
    children: list["Node"] = []


class tree(Occurrence[Node]):  # noqa: N801
    name = "tree"


def test_a_model_that_refers_to_itself_cannot_be_inlined() -> None:
    with pytest.raises(OutcomeError, match="Node refers to itself"):
        StateRef("testing", emits=(tree,)).outcome_schema()


def test_a_state_ref_is_typed_by_its_events() -> None:
    # assert_type does nothing at run time. Strict pyright (scripts/check.py) checks it.
    assert_type(testing, StateRef[passed | done])
    assert_type(testing.outcome({"event": "done"}), passed | done)
    assert str(vars(StateRef)["__parameters__"]) == "(~O,)"


def test_outcome_turns_an_answer_into_its_occurrence() -> None:
    happened = testing.outcome({"event": "passed", "payload": {"passed": 3, "failed": 0}})

    assert type(happened) is passed
    assert happened.payload == Report(passed=3, failed=0)
    assert type(testing.outcome({"event": "done"})) is done
    assert type(testing.outcome({"event": "done", "payload": None})) is done


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ("done", "an outcome of 'testing' is an object with 'event' and 'payload'"),
        ({}, "'testing' may not emit None"),
        ({"event": "stuck"}, "'testing' may not emit 'stuck'"),
        ({"event": "passed", "payload": {"passed": "x", "failed": 0}}, "does not fit Report"),
        ({"event": "passed"}, "the payload of 'passed' does not fit Report"),
        ({"event": "done", "payload": {"a": 1}}, "'done' carries no payload"),
        ({"event": "done", "extra": 1}, "not 'extra'"),
    ],
)
def test_an_answer_that_does_not_fit_is_refused(data: Any, reason: str) -> None:
    with pytest.raises(OutcomeError, match=reason) as raised:
        testing.outcome(data)

    assert "'testing' may emit 'passed', 'done'" in str(raised.value)


# ── on_state ──────────────────────────────────────────────────────────────


def test_on_state_labels_a_function_and_leaves_it_callable() -> None:
    @on_state(testing)
    def run_tests(ctx: object) -> None:
        return None

    assert implementation_of(run_tests) is testing
    assert run_tests(object()) is None


def test_an_unlabelled_function_implements_nothing() -> None:
    def plain() -> None: ...

    assert implementation_of(plain) is None


def test_the_state_must_come_from_a_contract() -> None:
    with pytest.raises(TypeError, match=r"a state from contract.states"):
        on_state("testing")  # type: ignore[arg-type]
