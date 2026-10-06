"""Stateous: a process as a graph of states.

A control plane keeps every token of it durable and decides each next state. A
supervisor, on any computer, does the work and reports what happened. Everything
a user imports comes from here.
"""

from stateous.model.workflow import END, START, Workflow

__all__ = ["END", "START", "Workflow"]
