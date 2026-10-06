"""The guard language: the `when=` conditions on transitions.

Small on purpose (docs/design.md, section 5.1). Python's `ast` module parses an
expression; then each node is checked against the grammar below. Nothing goes to
`eval`: an accepted guard is a tree of known nodes, and `evaluate` walks it.

    comparison   <  <=  >  >=  ==  !=  in  not in
    boolean      and  or  not
    literals     numbers (also negative), strings, true, false, null,
                 lists of literals
    accessors    ctx['key']         the token's context
                 payload.<field>    the payload of the event being routed
                 count('event')     accepted occurrences of the event on the
                                    token, the one being routed included

`in` and `not in` search a list literal or a value that an accessor reads, never
a string literal.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

_LITERAL_NAMES: dict[str, Any] = {"true": True, "false": False, "null": None}
_COMPARISONS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq, ast.In, ast.NotIn)


class GuardError(ValueError):
    """A guard expression is outside the guard language."""


@dataclass(frozen=True)
class Guard:
    """A parsed guard, and what it reads, so the compiler can check each against
    the schemas."""

    expression: str
    tree: ast.Expression
    context_keys: frozenset[str] = field(default_factory=frozenset[str])
    payload_fields: frozenset[str] = field(default_factory=frozenset[str])
    events: frozenset[str] = field(default_factory=frozenset[str])


def parse_guard(expression: str) -> Guard:
    """Parse and check `expression`. `GuardError` names what is outside the language."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        raise GuardError(f"guard {expression!r}: not a valid expression") from None
    checker = _Checker(expression)
    checker.check(tree.body)
    return Guard(
        expression=expression,
        tree=tree,
        context_keys=frozenset(checker.context_keys),
        payload_fields=frozenset(checker.payload_fields),
        events=frozenset(checker.events),
    )


def negative_number(node: ast.expr) -> int | float | None:
    """The value of a negative number literal, such as `-5`; else None."""
    match node:
        case ast.UnaryOp(op=ast.USub(), operand=ast.Constant(value=int() | float() as value)):
            return None if isinstance(value, bool) else -value
        case _:
            return None


def _searchable(node: ast.expr) -> bool:
    """What `in` may search: a list literal, or a value an accessor reads."""
    return isinstance(node, ast.List | ast.Subscript | ast.Attribute | ast.Call)


class _Checker:
    def __init__(self, expression: str) -> None:
        self.expression = expression
        self.context_keys: set[str] = set()
        self.payload_fields: set[str] = set()
        self.events: set[str] = set()

    def fail(self, reason: str) -> GuardError:
        return GuardError(f"guard {self.expression!r}: {reason}")

    def check(self, node: ast.expr) -> None:
        match node:
            case ast.BoolOp(values=values):
                for value in values:
                    self.check(value)
            case ast.UnaryOp(op=ast.Not(), operand=operand):
                self.check(operand)
            case ast.UnaryOp() if negative_number(node) is not None:
                pass
            case ast.Compare(left=left, ops=ops, comparators=comparators):
                if not all(isinstance(op, _COMPARISONS) for op in ops):
                    raise self.fail("compare with < <= > >= == != in or not in")
                for op, searched in zip(ops, comparators, strict=True):
                    if isinstance(op, ast.In | ast.NotIn) and not _searchable(searched):
                        raise self.fail(
                            "`in` must search a list literal or a value, e.g. ctx['key']"
                        )
                for operand in (left, *comparators):
                    self.check(operand)
            case ast.Constant(value=value):
                if not isinstance(value, str | int | float) or isinstance(value, bool):
                    raise self.fail(f"{value!r} is not a literal of the guard language")
            case ast.List(elts=elements):
                for element in elements:
                    literal = isinstance(element, ast.Constant | ast.Name)
                    if not literal and negative_number(element) is None:
                        raise self.fail("a list may only contain literals")
                    self.check(element)
            case ast.Name(id=name):
                self.name(name)
            case ast.Subscript():
                self.subscript(node)
            case ast.Attribute():
                self.attribute(node)
            case ast.Call():
                self.call(node)
            case ast.BinOp():
                raise self.fail("arithmetic is not part of the guard language")
            case ast.Lambda():
                raise self.fail("lambdas are not part of the guard language")
            case _:
                raise self.fail(f"{type(node).__name__} is not part of the guard language")

    def name(self, name: str) -> None:
        if name in _LITERAL_NAMES:
            return
        if name == "payload":
            raise self.fail("name a payload field, e.g. payload.severity")
        if name == "ctx":
            raise self.fail("read one key, e.g. ctx['key']")
        raise self.fail(f"unknown name {name!r}")

    def subscript(self, node: ast.Subscript) -> None:
        match node:
            case ast.Subscript(value=ast.Name(id="ctx"), slice=ast.Constant(value=str(key))):
                self.context_keys.add(key)
            case _:
                raise self.fail("only ctx['key'] may be indexed")

    def attribute(self, node: ast.Attribute) -> None:
        match node:
            case ast.Attribute(value=ast.Name(id="payload"), attr=attr):
                self.payload_fields.add(attr)
            case ast.Attribute(value=ast.Name(id="ctx")):
                raise self.fail("read context as ctx['key']")
            case _:
                raise self.fail("only payload.<field> may be read with a dot")

    def call(self, node: ast.Call) -> None:
        match node:
            case ast.Call(func=ast.Name(id="count"), args=args, keywords=keywords):
                if keywords:
                    raise self.fail("count() takes only the event name, e.g. count('stuck')")
                match args:
                    case [ast.Constant(value=str(event))]:
                        self.events.add(event)
                    case _:
                        raise self.fail(
                            "count() takes an event name as a string, e.g. count('stuck')"
                        )
            case _:
                raise self.fail("only count() may be called")


# ── Evaluation ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GuardInput:
    """All that a guard may read about a token at the moment an event is routed."""

    context: Mapping[str, Any]
    payload: Mapping[str, Any]
    counts: Mapping[str, int]
    """Event name -> accepted occurrences on the token, the one being routed included."""


def evaluate(guard: Guard, facts: GuardInput) -> bool:
    """Walk an accepted guard's tree. A missing value reads as null. Comparing null
    with a number is false, not an error: a guard answers yes or no."""
    return bool(_Evaluator(facts).value(guard.tree.body))


class _Evaluator:
    def __init__(self, facts: GuardInput) -> None:
        self.facts = facts

    def value(self, node: ast.expr) -> Any:
        match node:
            case ast.BoolOp(op=ast.And(), values=values):
                return all(self.value(v) for v in values)
            case ast.BoolOp(values=values):
                return any(self.value(v) for v in values)
            case ast.UnaryOp(op=ast.Not(), operand=operand):
                return not self.value(operand)
            case ast.UnaryOp() if (number := negative_number(node)) is not None:
                return number
            case ast.Compare(left=left, ops=ops, comparators=comparators):
                current = self.value(left)
                for op, comparator in zip(ops, comparators, strict=True):
                    following = self.value(comparator)
                    if not _compare(op, current, following):
                        return False
                    current = following
                return True
            case ast.Constant(value=value):
                return value
            case ast.List(elts=elements):
                return [self.value(e) for e in elements]
            case ast.Name(id=name):
                return _LITERAL_NAMES.get(name)
            case ast.Subscript(slice=ast.Constant(value=str(key))):
                return self.facts.context.get(key)
            case ast.Attribute(attr=attr):
                return self.facts.payload.get(attr)
            case ast.Call(args=[ast.Constant(value=str(event))]):
                return self.facts.counts.get(event, 0)
            case _:  # parse_guard accepts only the cases above
                raise GuardError(f"cannot evaluate {ast.dump(node)}")


def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    try:
        match op:
            case ast.Eq():
                return bool(left == right)
            case ast.NotEq():
                return bool(left != right)
            case ast.Lt():
                return bool(left < right)
            case ast.LtE():
                return bool(left <= right)
            case ast.Gt():
                return bool(left > right)
            case ast.GtE():
                return bool(left >= right)
            case ast.In():
                return right is not None and left in right
            case ast.NotIn():
                return right is not None and left not in right
            case _:
                return False
    except TypeError:
        return False
