"""Each concept page has the six parts, and the facts it must hold."""

from __future__ import annotations

import pytest

from .pages import page

PARTS = [
    "## In one sentence",
    "## Why it exists",
    "## How it works",
    "## Example",
    "## Guarantees",
    "## See also",
]
WORDS = [
    "workflow",
    "state",
    "event",
    "transition",
    "token",
    "context",
    "payload",
    "task",
    "routine",
    "supervisor",
    "control plane",
    "graph file",
    "contract",
    "ledger",
]

FACTS: dict[str, list[str]] = {
    "concepts/model.md": [f"### {word}" for word in WORDS]
    + ["START", "END", "otherwise=True", "```mermaid"],
    "concepts/two-sides.md": [
        "contract.py",
        "GET /workflow",
        "never sees the graph",
        "stateous init supervisor",
        "```mermaid",
    ],
    "concepts/tasks-and-routines.md": [
        "claim",
        "lease",
        "2 minutes",
        "heartbeat",
        "30 seconds",
        "attempt",
        "retries=",
        "`error`",
        "```mermaid",
    ],
    "concepts/guards.md": ["when=", "otherwise=True", "count(", "payload.", "ctx.", "overlap"],
    "concepts/timers.md": ["timeout=", "ctx.schedule", "`timeout`", "after="],
}


@pytest.mark.parametrize("name", sorted(FACTS))
def test_the_page_has_the_six_parts_in_order(name: str) -> None:
    text = page(name)
    positions = [text.index(part + "\n") for part in PARTS]
    assert positions == sorted(positions), name


@pytest.mark.parametrize("name", sorted(FACTS))
def test_the_page_holds_its_facts(name: str) -> None:
    text = page(name)
    for fact in FACTS[name]:
        assert fact in text, (name, fact)
