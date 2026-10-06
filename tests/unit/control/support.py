"""Helpers for the tests of the control plane."""

from __future__ import annotations

from typing import Any


def sdlc() -> dict[str, Any]:
    """The workflow of design.md section 4.1, as `stateous compile` writes it, with a
    `triage` state for urgent tickets. `coding` has transitions on `timeout` and
    `error`. `testing` has none: there, a timeout or a failed task fails the token."""
    return {
        "stateous": 1,
        "workflow": "sdlc",
        "context": "Ticket",
        "states": {
            "triage": {"emits": ["triaged"]},
            "coding": {"timeout": "6h", "retries": 2, "emits": ["done", "stuck"]},
            "testing": {"timeout": "1h", "emits": ["passed", "failed"]},
        },
        "events": {
            "triaged": {},
            "done": {},
            "stuck": {},
            "passed": {"payload": "Tests"},
            "failed": {"payload": "Tests"},
        },
        "transitions": [
            {"from": "__start__", "to": "triage", "when": "ctx['urgent'] == true"},
            {"from": "__start__", "to": "coding", "otherwise": True},
            {"from": "triage", "to": "coding", "on": "triaged"},
            {"from": "coding", "to": "testing", "on": "done"},
            {"from": "coding", "to": "__end__", "on": "stuck"},
            {"from": "coding", "to": "triage", "on": "timeout"},
            {"from": "coding", "to": "triage", "on": "error"},
            {"from": "testing", "to": "__end__", "on": "passed"},
            {"from": "testing", "to": "coding", "on": "failed", "when": "count('failed') < 3"},
            {"from": "testing", "to": "__end__", "on": "failed", "otherwise": True},
        ],
        "schemas": {
            "Ticket": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "rejected": {"type": "integer", "default": 0},
                    "urgent": {"type": "boolean", "default": False},
                },
                "required": ["repo"],
            },
            "Tests": {
                "type": "object",
                "properties": {"passed": {"type": "integer"}, "failed": {"type": "integer"}},
                "required": ["passed", "failed"],
            },
        },
    }
