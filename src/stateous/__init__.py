"""Stateous: a process as a graph of states.

A control plane keeps every token of it durable and decides each next state. A
supervisor, on any computer, does the work and reports what happened. Everything
a user imports comes from here.

The names of the control plane (`ControlPlane`, its errors and its results) load
when they are first used. So a routine that imports `on_state` does not load the
database libraries.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from stateous.client import Client
from stateous.model.branches import Branch, Branches
from stateous.model.context import Context
from stateous.model.contract import OutcomeError, StateRef, on_state
from stateous.model.events import Occurrence
from stateous.model.workflow import END, START, Workflow

if TYPE_CHECKING:
    from stateous.control.engine import Fired, HistoryEntry, Token
    from stateous.control.errors import (
        AttemptOver,
        ChildToken,
        ControlPlaneError,
        EventNotAllowed,
        GraphConflict,
        HoldLost,
        InvalidArgument,
        InvalidContext,
        InvalidPayload,
        InvalidTokenId,
        TaskNotFound,
        TaskNotHeld,
        TokenNotActive,
        TokenNotFound,
    )
    from stateous.control.plane import ControlPlane
    from stateous.control.store.errors import DatabaseHeld, ForeignDatabase, SchemaNotReady

_LAZY: dict[str, str] = {
    "ControlPlane": "stateous.control.plane",
    "Fired": "stateous.control.engine",
    "HistoryEntry": "stateous.control.engine",
    "Token": "stateous.control.engine",
    "DatabaseHeld": "stateous.control.store.errors",
    "ForeignDatabase": "stateous.control.store.errors",
    "SchemaNotReady": "stateous.control.store.errors",
    **{
        name: "stateous.control.errors"
        for name in (
            "AttemptOver",
            "ChildToken",
            "ControlPlaneError",
            "EventNotAllowed",
            "GraphConflict",
            "HoldLost",
            "InvalidArgument",
            "InvalidContext",
            "InvalidPayload",
            "InvalidTokenId",
            "TaskNotFound",
            "TaskNotHeld",
            "TokenNotActive",
            "TokenNotFound",
        )
    },
}


def __getattr__(name: str) -> Any:
    """Load a name of the control plane when it is first used."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'stateous' has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


__all__ = [
    "END",
    "START",
    "AttemptOver",
    "Branch",
    "Branches",
    "ChildToken",
    "Client",
    "Context",
    "ControlPlane",
    "ControlPlaneError",
    "DatabaseHeld",
    "EventNotAllowed",
    "Fired",
    "ForeignDatabase",
    "GraphConflict",
    "HistoryEntry",
    "HoldLost",
    "InvalidArgument",
    "InvalidContext",
    "InvalidPayload",
    "InvalidTokenId",
    "Occurrence",
    "OutcomeError",
    "SchemaNotReady",
    "StateRef",
    "TaskNotFound",
    "TaskNotHeld",
    "Token",
    "TokenNotActive",
    "TokenNotFound",
    "Workflow",
    "on_state",
]
