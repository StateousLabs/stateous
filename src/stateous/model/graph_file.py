"""The graph file: a compiled workflow as JSON text.

It is in `model`, not `compiler`, because the control plane reads graph files at
run time and may not import the compiler. It works on text only: the caller reads
and writes the file.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from stateous.model.durations import duration_problem

FORMAT = 1
"""The version of the graph file's own layout, as its `stateous` key."""

REQUIRED = ("stateous", "workflow", "states", "events", "transitions", "schemas")

OPERATIONAL = ("timeout", "retries")
"""A state's settings that change how its work is done, not where a token can go.
They are not part of `structure`."""


class GraphFileError(ValueError):
    """A graph file cannot be used as it is."""


def checksum(document: dict[str, Any]) -> str:
    """`sha256:` and the hash of the document's canonical JSON."""
    return _hash(document)


def structure(document: dict[str, Any]) -> str:
    """The same hash, of the document without each state's timeout and retries, in
    the main workflow and in each child workflow. Two graph files with equal
    structure differ only in operational settings."""
    shape = {**document, "states": _shape(document.get("states", {}))}
    children = cast("dict[str, dict[str, Any]]", document.get("workflows", {}))
    if children:
        shape["workflows"] = {
            name: {**body, "states": _shape(body.get("states", {}))}
            for name, body in children.items()
        }
    return _hash(shape)


def _shape(states: Any) -> dict[str, Any]:
    """States without their operational settings."""
    return {
        name: {key: value for key, value in options.items() if key not in OPERATIONAL}
        for name, options in cast("dict[str, dict[str, Any]]", states).items()
    }


def dump_graph(document: dict[str, Any]) -> str:
    """The graph file's text."""
    return json.dumps(document, indent=2, sort_keys=False) + "\n"


def load_graph(text: str) -> dict[str, Any]:
    """The document in a graph file's text. `GraphFileError`, one plain sentence,
    when the text is not a graph file this Stateous can read."""
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as error:
        raise GraphFileError(
            f"the graph file is not valid JSON: {error.msg} at line {error.lineno}"
        ) from None
    if not isinstance(loaded, dict):
        raise GraphFileError("the graph file is not a JSON object")
    document = cast("dict[str, Any]", loaded)
    for key in REQUIRED:
        if key not in document:
            raise GraphFileError(f"the graph file has no {key!r}; run `stateous compile` again")
    version = document["stateous"]
    if type(version) is not int or version != FORMAT:
        raise GraphFileError(
            f"the graph file has format {version!r}, but this Stateous reads format "
            f"{FORMAT}; run `stateous compile` again"
        )
    _refuse_bad_settings(document)
    return document


def _hash(document: dict[str, Any]) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _refuse_bad_settings(document: dict[str, Any]) -> None:
    """Each state's `timeout`, `retries`, `parallel` and `join`, in the main workflow
    and in each child workflow, checked with the rules the compiler uses. A file can
    carry one that the engine cannot use, e.g. after a hand edit."""
    children: Any = document.get("workflows", {})
    if not isinstance(children, dict):
        raise GraphFileError("the graph file's 'workflows' is not an object")
    workflows = cast("dict[str, Any]", children)
    _refuse_bad_states(document["states"], set(workflows))
    for name, body in workflows.items():
        states = cast("dict[str, Any]", body).get("states") if isinstance(body, dict) else None
        if states is None:
            raise GraphFileError(f"the graph file's workflow {name!r} has no 'states'")
        _refuse_bad_states(states, set(workflows))


def _refuse_bad_states(states: Any, workflows: set[str]) -> None:
    """The checks of `_refuse_bad_settings` for one workflow's states."""
    if not isinstance(states, dict):
        raise GraphFileError("the graph file's 'states' is not an object")
    for name, options in cast("dict[str, Any]", states).items():
        if not isinstance(options, dict):
            raise GraphFileError(f"the graph file's state {name!r} is not an object")
        settings = cast("dict[str, Any]", options)
        timeout = settings.get("timeout")
        if timeout is not None and (problem := duration_problem(timeout)):
            raise GraphFileError(f"the graph file's state {name!r}: {problem}")
        retries = settings.get("retries", 0)
        if type(retries) is not int or retries < 0:
            raise GraphFileError(
                f"the graph file's state {name!r}: retries is a whole number of 0 or "
                f"more, not {retries!r}"
            )
        parallel: Any = settings.get("parallel")
        if parallel is None:
            continue
        named = cast("list[Any]", parallel) if isinstance(parallel, list) else []
        if not named or any(not isinstance(c, str) or c not in workflows for c in named):
            raise GraphFileError(
                f"the graph file's state {name!r}: 'parallel' names a workflow that "
                "'workflows' does not have"
            )
        if settings.get("join", "all") not in ("all", "any"):
            raise GraphFileError(f"the graph file's state {name!r}: 'join' is 'all' or 'any'")
