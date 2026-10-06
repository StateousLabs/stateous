"""Durations, as `timeout=` and `ctx.schedule(after=...)` take them."""

from datetime import timedelta
from typing import Any

import pytest

from stateous.model.durations import duration_problem, parse_duration


@pytest.mark.parametrize("text", ["30s", "10m", "6h", "1d", "3650d", "315360000s"])
def test_a_number_and_a_unit_is_a_duration(text: str) -> None:
    assert duration_problem(text) is None


@pytest.mark.parametrize("text", ["an hour", "0s", "1w", "1.5h", "", "30s\n", "-5m", "10"])
def test_anything_else_is_not_a_duration(text: str) -> None:
    problem = duration_problem(text)

    assert problem is not None
    assert "is not a duration; write e.g. '30s', '10m', '2h', '1d'" in problem


@pytest.mark.parametrize("text", ["3651d", "3000000d", "315360001s"])
def test_a_duration_is_at_most_ten_years(text: str) -> None:
    assert duration_problem(text) == f"{text!r} is too long; a duration is at most 3650d"


@pytest.mark.parametrize("value", [60, None, 1.5])
def test_a_value_that_is_not_text_is_not_a_duration(value: Any) -> None:
    expected = f"{value!r} is not a duration; write e.g. '30s', '10m', '2h', '1d'"

    assert duration_problem(value) == expected


def test_parse_duration_gives_a_timedelta() -> None:
    assert parse_duration("30s") == timedelta(seconds=30)
    assert parse_duration("10m") == timedelta(minutes=10)
    assert parse_duration("6h") == timedelta(hours=6)
    assert parse_duration("2d") == timedelta(days=2)


def test_parse_duration_refuses_what_is_not_a_duration() -> None:
    with pytest.raises(ValueError, match="'soon' is not a duration"):
        parse_duration("soon")
