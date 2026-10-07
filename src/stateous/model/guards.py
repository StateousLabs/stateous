"""The guard language: the `when=` conditions on transitions.

Small on purpose (docs/design.md, sections 5.1 and 8). Python's `ast` module parses
an expression; then each node is checked against the grammar below. Nothing goes to
`eval`: an accepted guard is a tree of known nodes, and `evaluate` walks it.

    comparison   <  <=  >  >=  ==  !=  in  not in
    boolean      and  or  not
    literals     numbers (also negative), strings, true, false, null,
                 lists of literals
    accessors    ctx['key']         the token's context
                 payload.<field>    the payload of the event being routed
                 count('event')     accepted occurrences of the event on the
                                    token, the one being routed included
    children     branches.all(c)    on `joined` only: c holds for each ended child
                 branches.any(c)    on `joined` only: c holds for one ended child
                                    In c: workflow, token, status, final and
                                    payload.<field> of that child

`in` and `not in` search a list literal or a value that an accessor reads, never
a string literal.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

_LITERAL_NAMES: dict[str, Any] = {"true": True, "false": False, "null": None}
_COMPARISONS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq, ast.In, ast.NotIn)

BRANCH_FIELDS = frozenset({"workflow", "token", "status", "final"})
"""What a condition in `branches.all(...)` or `branches.any(...)` reads of one ended
child, besides `payload.<field>`: one entry of the `joined` payload."""


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
    """The fields of the routed event's payload. A child's payload is not in it."""
    events: frozenset[str] = field(default_factory=frozenset[str])
    branches: bool = False
    """True when the guard reads `branches.all(...)` or `branches.any(...)`."""


def parse_guard(expression: str) -> Guard:
    """Parse and check `expression`. `GuardError` names what is outside the language."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        raise GuardError(f"guard {expression!r}: not a valid expression") from None
    checker = _Checker(expression)
    checker.check(tree.body, in_branch=False)
    return Guard(
        expression=expression,
        tree=tree,
        context_keys=frozenset(checker.context_keys),
        payload_fields=frozenset(checker.payload_fields),
        events=frozenset(checker.events),
        branches=checker.branches,
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
        self.branches = False

    def fail(self, reason: str) -> GuardError:
        return GuardError(f"guard {self.expression!r}: {reason}")

    def check(self, node: ast.expr, *, in_branch: bool) -> None:
        """Check one node. `in_branch`: inside `branches.all(...)` or `.any(...)`,
        where the names of BRANCH_FIELDS read one child."""
        match node:
            case ast.BoolOp(values=values):
                for value in values:
                    self.check(value, in_branch=in_branch)
            case ast.UnaryOp(op=ast.Not(), operand=operand):
                self.check(operand, in_branch=in_branch)
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
                    self.check(operand, in_branch=in_branch)
            case ast.Constant(value=value):
                if not isinstance(value, str | int | float) or isinstance(value, bool):
                    raise self.fail(f"{value!r} is not a literal of the guard language")
            case ast.List(elts=elements):
                for element in elements:
                    literal = isinstance(element, ast.Constant | ast.Name)
                    if not literal and negative_number(element) is None:
                        raise self.fail("a list may only contain literals")
                    self.check(element, in_branch=in_branch)
            case ast.Name(id=name):
                self.name(name, in_branch=in_branch)
            case ast.Subscript():
                self.subscript(node)
            case ast.Attribute():
                self.attribute(node, in_branch=in_branch)
            case ast.Call():
                self.call(node, in_branch=in_branch)
            case ast.BinOp():
                raise self.fail("arithmetic is not part of the guard language")
            case ast.Lambda():
                raise self.fail("lambdas are not part of the guard language")
            case _:
                raise self.fail(f"{type(node).__name__} is not part of the guard language")

    def name(self, name: str, *, in_branch: bool) -> None:
        if name in _LITERAL_NAMES or (in_branch and name in BRANCH_FIELDS):
            return
        if name == "payload":
            raise self.fail("name a payload field, e.g. payload.severity")
        if name == "ctx":
            raise self.fail("read one key, e.g. ctx['key']")
        if name in BRANCH_FIELDS:
            raise self.fail(
                f"{name!r} is a field of a child; read it in branches.all(...) or branches.any(...)"
            )
        raise self.fail(f"unknown name {name!r}")

    def subscript(self, node: ast.Subscript) -> None:
        match node:
            case ast.Subscript(value=ast.Name(id="ctx"), slice=ast.Constant(value=str(key))):
                self.context_keys.add(key)
            case _:
                raise self.fail("only ctx['key'] may be indexed")

    def attribute(self, node: ast.Attribute, *, in_branch: bool) -> None:
        match node:
            case ast.Attribute(value=ast.Name(id="payload"), attr=attr):
                if not in_branch:  # a child's payload is not the routed event's payload
                    self.payload_fields.add(attr)
            case ast.Attribute(value=ast.Name(id="ctx")):
                raise self.fail("read context as ctx['key']")
            case _:
                raise self.fail("only payload.<field> may be read with a dot")

    def call(self, node: ast.Call, *, in_branch: bool) -> None:
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
            case ast.Call(
                func=ast.Attribute(value=ast.Name(id="branches"), attr="all" | "any"),
                args=[condition],
                keywords=[],
            ) if not in_branch:
                self.branches = True
                self.check(condition, in_branch=True)
            case ast.Call(func=ast.Attribute(value=ast.Name(id="branches"), attr="all" | "any")):
                raise self.fail(
                    "branches.all() and branches.any() take one condition, and do not nest"
                )
            case _:
                raise self.fail("only count(), branches.all() and branches.any() may be called")


# ── Evaluation ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GuardInput:
    """All that a guard may read about a token at the moment an event is routed."""

    context: Mapping[str, Any]
    payload: Mapping[str, Any]
    counts: Mapping[str, int]
    """Event name -> accepted occurrences on the token, the one being routed included."""
    branches: Sequence[Mapping[str, Any]] = ()
    """For `joined`: one entry for each ended child, with `workflow`, `token`,
    `status`, `final` and `payload`. Empty for every other event."""


def evaluate(guard: Guard, facts: GuardInput) -> bool:
    """Walk an accepted guard's tree. A missing value reads as null. Comparing null
    with a number is false, not an error: a guard answers yes or no."""
    return bool(_Evaluator(facts).value(guard.tree.body, branch=None))


class _Evaluator:
    def __init__(self, facts: GuardInput) -> None:
        self.facts = facts

    def value(self, node: ast.expr, *, branch: Mapping[str, Any] | None) -> Any:
        """The value of `node`. `branch`: the child that a condition in
        `branches.all(...)` or `branches.any(...)` reads now; None outside one."""
        match node:
            case ast.BoolOp(op=ast.And(), values=values):
                return all(self.value(v, branch=branch) for v in values)
            case ast.BoolOp(values=values):
                return any(self.value(v, branch=branch) for v in values)
            case ast.UnaryOp(op=ast.Not(), operand=operand):
                return not self.value(operand, branch=branch)
            case ast.UnaryOp() if (number := negative_number(node)) is not None:
                return number
            case ast.Compare(left=left, ops=ops, comparators=comparators):
                current = self.value(left, branch=branch)
                for op, comparator in zip(ops, comparators, strict=True):
                    following = self.value(comparator, branch=branch)
                    if not _compare(op, current, following):
                        return False
                    current = following
                return True
            case ast.Constant(value=value):
                return value
            case ast.List(elts=elements):
                return [self.value(e, branch=branch) for e in elements]
            case ast.Name(id=name):
                if name in _LITERAL_NAMES:
                    return _LITERAL_NAMES[name]
                return None if branch is None else branch.get(name)
            case ast.Subscript(slice=ast.Constant(value=str(key))):
                return self.facts.context.get(key)
            case ast.Attribute(attr=attr):
                payload = self.facts.payload if branch is None else _payload_of(branch)
                return payload.get(attr)
            case ast.Call(func=ast.Name(id="count"), args=[ast.Constant(value=str(event))]):
                return self.facts.counts.get(event, 0)
            case ast.Call(func=ast.Attribute(attr=quantifier), args=[condition]):
                results = (self.value(condition, branch=child) for child in self.facts.branches)
                return all(results) if quantifier == "all" else any(results)
            case _:  # parse_guard accepts only the cases above
                raise GuardError(f"cannot evaluate {ast.dump(node)}")


def _payload_of(branch: Mapping[str, Any]) -> Mapping[str, Any]:
    """A child's payload; an empty one when its event carried none."""
    payload = branch.get("payload")
    return cast("Mapping[str, Any]", payload) if isinstance(payload, Mapping) else {}


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
