"""Proving that two guards can never both be true.

Two transitions that could both match are a compile error, never a choice at run
time (see the guide, "Guards"). The guard language is small enough to decide it.

A guard reads a few values (context keys, payload fields, event counts) and
compares each with literals. Between and around those literals a value behaves
the same. So one value from each region (each literal, a point just past it on
each side, and a value beyond them all) stands for every value there is. Each
value also stays in what its schema allows: an `int` is never 3.5, a
`Literal['high', 'low']` is never 'medium', a count is never negative. Then the
engine's own evaluator evaluates the guards, so an example found is a real one.

A value read as true or false is also compared with 0, so a number on each side
of 0 is tried. A list and an object are each tried empty and not empty. A list
literal is one value, except where `in` searches it.

A guard that compares two values with each other, orders a list, or reads
`branches.all` or `branches.any` cannot be decided, and is refused: a guard that
cannot be proven exclusive is not.
"""

from __future__ import annotations

import ast
import itertools
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel, PydanticUserError

from stateous.model.guards import Guard, GuardInput, evaluate, negative_number

MAX_TRIES = 200_000
"""How many combinations of values to try before a pair is called undecidable."""

Atom = tuple[str, ...]
"""What a guard reads: ("ctx", key) · ("payload", field) · ("count", event)."""

_OBJECT: dict[str, Any] = {"__object__": True}
"""Stands for any object value: equal to no literal, ordered with none."""

_BRANCHES = "a guard with branches.all or branches.any cannot be proven exclusive"
"""Why a pair with a branch quantifier cannot be decided: it reads a list of children
of any length. stateous-flow refused it the same way."""


@dataclass(frozen=True)
class Verdict:
    exclusive: bool
    example: dict[str, Any] | None = None
    """When both can be true: values that make them so, by what each reads."""
    undecidable: str | None = None
    """When it cannot be decided: why."""


@dataclass(frozen=True)
class Domain:
    """The values a guard's operand can take, as its schema says."""

    kinds: frozenset[str]
    """Any of: int, number, string, bool, null, object, array, or any."""
    enum: tuple[Any, ...] | None = None
    minimum: int | None = None


ANY = Domain(frozenset({"any"}))


@dataclass(frozen=True)
class Where:
    """Where a pair of guards is: the event they share and the schemas they read."""

    event: str
    context: type[BaseModel] | None
    payload: type[BaseModel] | None

    def domain(self, atom: Atom) -> Domain:
        match atom:
            case ("ctx", key):
                return _field_domain(self.context, key)
            case ("payload", name):
                return _field_domain(self.payload, name)
            case _:  # ("count", event): the event being routed is counted already
                return Domain(frozenset({"int"}), minimum=1 if atom[1] == self.event else 0)


def check(first: Guard, second: Guard, where: Where) -> Verdict:
    """Whether `first` and `second` can never both be true."""
    reads: dict[Atom, list[Any]] = {}  # lists: a set would merge 1 and True
    for guard in (first, second):
        problem = _collect(guard.tree.body, reads)
        if problem is not None:
            return Verdict(False, undecidable=problem)

    atoms = sorted(reads)
    domains = [where.domain(atom) for atom in atoms]
    for atom, domain in zip(atoms, domains, strict=True):
        searched = any(isinstance(c, tuple) for c in reads[atom])
        if searched and not domain.kinds & {"array", "any"}:
            return Verdict(False, undecidable=f"`in` searches {_label(atom)}, which is not a list")
    choices = [_candidates(d, reads[a]) for a, d in zip(atoms, domains, strict=True)]
    if math.prod(len(c) for c in choices) > MAX_TRIES:
        return Verdict(False, undecidable="they read too many values to try every case")
    for values in itertools.product(*choices):
        facts = _facts(dict(zip(atoms, values, strict=True)))
        if evaluate(first, facts) and evaluate(second, facts):
            example = {_label(a): v for a, v in zip(atoms, values, strict=True)}
            return Verdict(False, example=example)
    return Verdict(True)


# ── what a guard reads, and the literals it compares each with ──────────────


def _collect(node: ast.expr, reads: dict[Atom, list[Any]]) -> str | None:
    """Record each value the guard reads and the literals it is compared with.
    Returns the reason the pair cannot be decided, if it cannot."""
    match node:
        case ast.BoolOp(values=values):
            for value in values:
                if (problem := _collect(value, reads)) is not None:
                    return problem
            return None
        case ast.UnaryOp(operand=operand):
            return _collect(operand, reads)
        case ast.Compare(left=left, ops=ops, comparators=comparators):
            operands = [left, *comparators]
            for i, op in enumerate(ops):
                problem = _pair(operands[i], op, operands[i + 1], reads)
                if problem is not None:
                    return problem
            return None
        case ast.Call(func=ast.Attribute(value=ast.Name(id="branches"))):
            return _BRANCHES
        case _:
            atom = _atom(node)
            if atom is not None:  # a value used as a truth value, e.g. `ctx['flag']`
                reads.setdefault(atom, []).extend([True, False, 0])
            return None


def _pair(
    left: ast.expr, op: ast.cmpop, right: ast.expr, reads: dict[Atom, list[Any]]
) -> str | None:
    if _is_branch(left) or _is_branch(right):
        return _BRANCHES
    left_atom, right_atom = _atom(left), _atom(right)
    if left_atom is not None and right_atom is not None:
        return "a guard compares two values with each other"
    searched = isinstance(op, ast.In | ast.NotIn)
    literals: list[Any] = []
    for side in (left, right):
        if _atom(side) is None:
            value = _literal(side)
            if isinstance(value, list) and not (searched and side is right):
                if not isinstance(op, ast.Eq | ast.NotEq | ast.In | ast.NotIn):
                    return "a guard orders a list"
                literals.append(value)  # one value, e.g. ctx['tags'] == ['a']
            elif isinstance(value, list):
                literals.extend(cast("list[Any]", value))  # what `in` searches
            elif value is not _NOTHING:
                literals.append(value)
    for atom in (left_atom, right_atom):
        if atom is not None:
            if isinstance(op, ast.In | ast.NotIn) and atom is right_atom:
                reads.setdefault(atom, []).append(("in", tuple(literals)))
            else:
                reads.setdefault(atom, []).extend(literals)
    return None


def _is_branch(node: ast.expr) -> bool:
    """Whether `node` is `branches.all(...)` or `branches.any(...)`."""
    match node:
        case ast.Call(func=ast.Attribute(value=ast.Name(id="branches"))):
            return True
        case _:
            return False


def _atom(node: ast.expr) -> Atom | None:
    match node:
        case ast.Subscript(value=ast.Name(id="ctx"), slice=ast.Constant(value=str(key))):
            return ("ctx", key)
        case ast.Attribute(value=ast.Name(id="payload"), attr=name):
            return ("payload", name)
        case ast.Call(func=ast.Name(id="count"), args=[ast.Constant(value=str(event))]):
            return ("count", event)
        case _:
            return None


_NOTHING = object()
"""What `_literal` gives for a node that is not a literal."""


def _literal(node: ast.expr) -> Any:
    """The value of a literal node, a list literal as a list; else `_NOTHING`."""
    match node:
        case ast.Constant(value=value):
            return value
        case ast.Name(id="true"):
            return True
        case ast.Name(id="false"):
            return False
        case ast.Name(id="null"):
            return None
        case ast.List(elts=elements):
            return [_literal(e) for e in elements]
        case _:
            number = negative_number(node)
            return _NOTHING if number is None else number


# ── the values worth trying ─────────────────────────────────────────────────


def _candidates(domain: Domain, compared: list[Any]) -> list[Any]:
    """One value from each region that the literals divide the domain into."""
    containers: list[tuple[Any, ...]] = [
        cast("tuple[str, tuple[Any, ...]]", c)[1] for c in compared if isinstance(c, tuple)
    ]
    literals = [c for c in compared if not isinstance(c, tuple)]
    numbers = sorted({v for v in literals if _is_number(v)})
    strings = sorted({v for v in literals if isinstance(v, str)})
    lists = [cast("list[Any]", v) for v in literals if isinstance(v, list)]
    kinds = domain.kinds
    tries: list[Any] = []

    if domain.enum is not None:
        tries += list(domain.enum)
    else:
        if kinds & {"int", "any"}:
            around = {k for n in numbers for k in range(math.floor(n) - 1, math.ceil(n) + 2)}
            ints = sorted(around | {0})
            if domain.minimum is not None:
                ints = [i for i in ints if i >= domain.minimum] or [domain.minimum]
            tries += ints
        if kinds & {"number", "any"}:
            points = sorted(set(numbers) | {0})
            between = [(a + b) / 2 for a, b in itertools.pairwise(points)]
            tries += sorted({*points, *between, points[0] - 1, points[-1] + 1})
        if kinds & {"string", "any"}:
            tries += ["", *strings, *(s + "\0" for s in strings)]
            if not strings:
                tries.append("x")
        if kinds & {"bool", "any"}:
            tries += [False, True]
        if kinds & {"object", "any"}:
            tries += [_OBJECT, {}]
        if kinds & {"array", "any"}:
            tries += _lists(_distinct(v for group in containers for v in group), lists)
    if kinds & {"null", "any"}:
        tries.append(None)
    return _distinct(tries)


def _lists(members: list[Any], literals: list[list[Any]]) -> list[Any]:
    """Lists that `in` and `==` tell apart: each list literal, for each set of the
    searched members a list that holds just those and equals no literal, and a list
    that is not empty but holds no member and equals no literal."""
    known = [*members, *(v for literal in literals for v in literal)]
    filler = "~"
    while filler in known:
        filler += "~"
    tries: list[Any] = [*literals]
    for r in range(len(members) + 1):
        for chosen in itertools.combinations(members, r):
            value = list(chosen)
            tries.append(value if value not in literals else [*value, filler])
    tries.append([filler])
    return tries


def _distinct(values: Iterable[Any]) -> list[Any]:
    """Without repeats, and with 1 kept apart from True and 0 from False."""
    seen: set[str] = set()
    kept: list[Any] = []
    for value in values:
        key = f"{type(value).__name__}:{value!r}"
        if key not in seen:
            seen.add(key)
            kept.append(value)
    return kept


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _facts(values: Mapping[Atom, Any]) -> GuardInput:
    context: dict[str, Any] = {}
    payload: dict[str, Any] = {}
    counts: dict[str, int] = {}
    for atom, value in values.items():
        match atom:
            case ("ctx", key):
                context[key] = value
            case ("payload", name):
                payload[name] = value
            case _:
                counts[atom[1]] = value
    return GuardInput(context, payload, counts)


def _label(atom: Atom) -> str:
    match atom:
        case ("ctx", key):
            return f"ctx[{key!r}]"
        case ("payload", name):
            return f"payload.{name}"
        case _:
            return f"count({atom[1]!r})"


# ── what a schema allows ────────────────────────────────────────────────────


def _field_domain(model: type[BaseModel] | None, name: str) -> Domain:
    if model is None:
        return ANY
    try:
        schema = model.model_json_schema()
    except PydanticUserError:  # validate reports it; any value is a safe guess here
        return ANY
    prop = cast("dict[str, Any] | None", schema.get("properties", {}).get(name))
    if prop is None:
        return ANY
    return _domain(prop, cast("Mapping[str, Any]", schema.get("$defs", {})))


def _domain(schema: Mapping[str, Any], definitions: Mapping[str, Any]) -> Domain:
    """The values `schema` allows. A `$ref` (pydantic writes one for an Enum or a
    model) is read through `definitions`."""
    ref = schema.get("$ref")
    if isinstance(ref, str):
        target = definitions.get(ref.rsplit("/", 1)[-1])
        if target is None:
            return ANY
        rest = {key: value for key, value in schema.items() if key != "$ref"}
        return _domain({**cast("Mapping[str, Any]", target), **rest}, definitions)
    for combined in ("anyOf", "oneOf"):
        if combined in schema:
            parts = [_domain(cast("Mapping[str, Any]", s), definitions) for s in schema[combined]]
            if any(p.kinds == ANY.kinds for p in parts):
                return ANY
            enums = [p.enum for p in parts if p.enum is not None]
            kinds = frozenset(k for p in parts for k in p.kinds)
            if enums and all(p.enum is not None or p.kinds == frozenset({"null"}) for p in parts):
                return Domain(kinds, enum=tuple(v for e in enums for v in e))
            return Domain(kinds)
    if "enum" in schema or "const" in schema:
        values = tuple(schema["enum"]) if "enum" in schema else (schema["const"],)
        return Domain(frozenset(_kind_of(v) for v in values) - {"null"}, enum=values)
    kinds = schema.get("type")
    if kinds is None:
        return ANY
    names = [kinds] if isinstance(kinds, str) else list(cast("list[str]", kinds))
    mapping = {
        "integer": "int",
        "number": "number",
        "string": "string",
        "boolean": "bool",
        "null": "null",
        "object": "object",
        "array": "array",
    }
    return Domain(frozenset(mapping.get(n, "any") for n in names))


def _kind_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "number"
    return "string"
