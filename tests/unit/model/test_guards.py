"""The guard language: what it accepts, what it refuses, and what a guard says."""

from typing import Any

import pytest

from stateous.model.guards import GuardError, GuardInput, evaluate, parse_guard

# ── the grammar ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "expression",
    [
        "count('failed') < 3",
        "count('timeout') < 2",
        "ctx['brief'] == null",
        "payload.severity in ['high', 'critical']",
        "not (ctx['points'] >= 2)",
        "ctx['pr_number'] != null or true",
        "count('review_done') >= 2 and ctx['rejected'] == 0",
        "1 < count('failed') < 3",
        "ctx['delta'] > -5",
        "ctx['delta'] in [-1, 2.5]",
        "payload.severity in ctx['levels']",
    ],
)
def test_the_grammar_accepts(expression: str) -> None:
    parse_guard(expression)


@pytest.mark.parametrize(
    ("expression", "reason"),
    [
        ("__import__('os')", r"only count\(\) may be called"),
        ("ctx.brief", r"read context as ctx\['key'\]"),
        ("count('a') + 1 < 3", "arithmetic is not part of the guard language"),
        ("count(name) < 3", r"count\(\) takes an event name as a string"),
        ("count('a', state='s') < 3", r"count\(\) takes only the event name"),
        ("payload", "name a payload field, e.g. payload.severity"),
        ("ctx", r"read one key, e.g. ctx\['key'\]"),
        ("x == 1", "unknown name 'x'"),
        ("visit > 1", "unknown name 'visit'"),
        ("attempt > 1", "unknown name 'attempt'"),
        ("tenant['team'] == 'x'", r"only ctx\['key'\] may be indexed"),
        ("branches.all(final == 'approved')", r"only count\(\) may be called"),
        ("count('a') <", "not a valid expression"),
        ("lambda: 1", "lambdas are not part of the guard language"),
        ("ctx['a'] == [ctx['b']]", "a list may only contain literals"),
        ("payload.s in 'abc'", "search a list literal or a value"),
        ("payload.s not in 'abc'", "search a list literal or a value"),
        ("1 in 3", "search a list literal or a value"),
        ("-ctx['a'] < 3", "UnaryOp is not part of the guard language"),
        ("-'a' == ctx['a']", "UnaryOp is not part of the guard language"),
    ],
)
def test_the_grammar_refuses(expression: str, reason: str) -> None:
    with pytest.raises(GuardError, match=reason):
        parse_guard(expression)


def test_a_refusal_names_the_guard() -> None:
    with pytest.raises(GuardError, match=r"^guard 'x == 1': "):
        parse_guard("x == 1")


def test_a_guard_records_what_it_reads() -> None:
    guard = parse_guard(
        "ctx['brief'] == null and payload.severity == 'high' and count('failed') < 3"
    )

    assert guard.expression == (
        "ctx['brief'] == null and payload.severity == 'high' and count('failed') < 3"
    )
    assert guard.context_keys == {"brief"}
    assert guard.payload_fields == {"severity"}
    assert guard.events == {"failed"}


# ── evaluation ────────────────────────────────────────────────────────────


def given(**overrides: Any) -> GuardInput:
    base: dict[str, Any] = {
        "context": {"brief": None, "repo": "acme/api", "rejected": 0},
        "payload": {"severity": "high"},
        "counts": {"failed": 2, "timeout": 1, "review_done": 2},
    }
    base.update(overrides)
    return GuardInput(**base)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("count('failed') < 3", True),
        ("count('failed') >= 3", False),
        ("count('timeout') < 2", True),
        ("count('never_happened') == 0", True),
        ("ctx['brief'] == null", True),
        ("ctx['repo'] == 'acme/api'", True),
        ("payload.severity in ['high', 'critical']", True),
        ("payload.severity not in ['high', 'critical']", False),
        ("not (count('failed') > 1) or ctx['repo'] != null", True),
        ("1 < count('failed') < 3", True),
        ("count('review_done') >= 2 and ctx['rejected'] == 0", True),
        ("ctx['rejected'] > -5", True),
        ("ctx['rejected'] < -5", False),
        ("ctx['rejected'] not in [-1, -2]", True),
        ("not ctx['rejected'] > -1", False),
    ],
)
def test_guards_evaluate_against_what_a_token_knows(expression: str, expected: bool) -> None:
    assert evaluate(parse_guard(expression), given()) is expected


def test_a_missing_value_is_null_not_an_error() -> None:
    assert evaluate(parse_guard("ctx['nothing_here'] == null"), given())
    assert evaluate(parse_guard("payload.missing == null"), given())


def test_comparing_null_with_a_number_is_false_not_an_error() -> None:
    assert not evaluate(parse_guard("ctx['brief'] > 3"), given())
    assert not evaluate(parse_guard("'x' in ctx['brief']"), given())
