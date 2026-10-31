"""The data of a join: what the event `joined` carries for each ended child of a
parallel state (see the docs, Concepts > Parallel and nested work).

A parallel state with `into="key"` keeps this payload in the parent's context, so
the context field is typed `Branches`, or `Branches | None`."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class Branch(BaseModel):
    """One ended child of a parallel state."""

    workflow: str
    """The child's workflow."""
    token: str
    """The child's id."""
    status: Literal["completed", "failed"]
    final: str
    """The state that the child was in when it ended."""
    context: dict[str, Any]
    """The child's context when it ended."""


class Branches(BaseModel):
    """The payload of `joined`: with `join="all"`, one entry for each child; with
    `join="any"`, the child that ended first."""

    branches: list[Branch]
