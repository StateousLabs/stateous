"""The values that a guard can read: what the context model allows for each key."""

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from stateous.compiler.exclusive import ANY, Domain, Where, check
from stateous.model.guards import parse_guard


def verdict_of(first: str, second: str, context: type[BaseModel] | None = None) -> Any:
    return check(parse_guard(first), parse_guard(second), Where("e", context, None))


def test_a_pair_with_a_branch_quantifier_inside_and_is_undecidable() -> None:
    verdict = verdict_of("ctx['a'] == 1 and branches.all(final == 'x')", "ctx['a'] == 2")

    assert verdict.undecidable == (
        "a guard with branches.all or branches.any cannot be proven exclusive"
    )


def test_a_guard_that_reads_too_many_values_is_undecidable() -> None:
    reads = " and ".join(f"ctx['k{i}'] == {i}" for i in range(6))

    verdict = verdict_of(reads, "ctx['k0'] == 99")

    assert verdict.undecidable == "they read too many values to try every case"


def test_a_list_literal_that_holds_the_filler_value_still_gets_a_distinct_list() -> None:
    verdict = verdict_of("ctx['t'] == ['~']", "ctx['t'] != ['~']")

    assert verdict.exclusive


def test_a_value_without_a_model_may_be_anything() -> None:
    assert Where("e", None, None).domain(("ctx", "x")) == ANY


class Spoiled(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    kept: int

    class Inner:
        pass

    inner: Inner


def test_a_model_without_a_json_schema_gives_any_value() -> None:
    assert Where("e", Spoiled, None).domain(("ctx", "kept")) == ANY


class Mixed(BaseModel):
    free: Any
    either: int | Any
    maybe: Literal["a", "b"] | None = None
    flag: Literal[True] = True
    whole: Literal[3] = 3


class Weight(float, Enum):
    LIGHT = 1.5
    HEAVY = 2.5


class Scale(BaseModel):
    weight: Weight


def test_a_field_without_a_type_gives_any_value() -> None:
    assert Where("e", Mixed, None).domain(("ctx", "free")) == ANY


def test_a_union_with_a_free_part_gives_any_value() -> None:
    assert Where("e", Mixed, None).domain(("ctx", "either")) == ANY


def test_a_field_the_model_does_not_have_gives_any_value() -> None:
    assert Where("e", Mixed, None).domain(("ctx", "nothing")) == ANY


def test_an_optional_literal_keeps_its_values_and_allows_null() -> None:
    domain = Where("e", Mixed, None).domain(("ctx", "maybe"))

    assert isinstance(domain, Domain)
    assert domain.enum == ("a", "b")
    assert domain.kinds == frozenset({"string", "null"})


def test_the_kind_of_each_literal_value_is_read() -> None:
    where = Where("e", Mixed, None)

    assert where.domain(("ctx", "flag")).kinds == frozenset({"bool"})
    assert where.domain(("ctx", "whole")).kinds == frozenset({"int"})


def test_a_float_enum_has_the_number_kind() -> None:
    assert Where("e", Scale, None).domain(("ctx", "weight")).kinds == frozenset({"number"})


def test_a_literal_none_alone_has_the_null_kind() -> None:
    class OnlyNone(BaseModel):
        value: Literal[None] = None

    domain = Where("e", OnlyNone, None).domain(("ctx", "value"))

    assert domain.enum == (None,)
    assert domain.kinds == frozenset()
