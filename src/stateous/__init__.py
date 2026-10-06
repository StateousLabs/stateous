"""Stateous: a process as a graph of states.

A control plane keeps every token of it durable and decides each next state. A
supervisor, on any computer, does the work and reports what happened. Everything
a user imports comes from here.
"""

from stateous.client import Client
from stateous.model.context import Context
from stateous.model.contract import OutcomeError, StateRef, on_state
from stateous.model.events import Occurrence
from stateous.model.workflow import END, START, Workflow

__all__ = [
    "END",
    "START",
    "Client",
    "Context",
    "Occurrence",
    "OutcomeError",
    "StateRef",
    "Workflow",
    "on_state",
]
