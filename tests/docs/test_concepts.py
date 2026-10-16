"""Each concept page has the six parts, and the facts it must hold."""

from __future__ import annotations

import inspect
import re

import pytest

from stateous import Context

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
    "concepts/context-notes-memory.md": [
        'ctx["key"]',
        "ctx.note",
        "ctx.memory",
        "500",
        "deleted when the token ends",
        "never the value",
    ],
    "concepts/pause-resume-cancel.md": [
        "held",
        "arrival",
        "stateous token pause",
        "stateous token resume",
        "stateous token cancel",
        "409",
    ],
    "concepts/parallel.md": [
        "parallel=[",
        'join="all"',
        'join="any"',
        "inputs=[",
        "`joined`",
        "branches.all(",
        "branches.any(",
        "`<parent>.<state>.<visit>.<workflow>`",
        "`<workflow>/<state>`",
        "`GET /tokens?parent=<id>`",
        "409",
        "```mermaid",
    ],
    "concepts/reliability.md": [
        "**The ledger**",
        "**Leases**",
        "**The clock**",
        "**Idempotency keys**",
        "**The routine's memory**",
        "**The supervisor's outbox**",
        "**Pause and resume**",
        "Idempotency-Key",
        "statement_timeout",
        "2 minutes",
        "```mermaid",
    ],
    "concepts/agents.md": [
        "stateous[phd]",
        "poly-harness-dispatcher",
        "Dispatcher(",
        ".run(",
        "handle.session_id",
        "idle_timeout_s",
        "event.summary",
        "phd.ERROR_KINDS",
        "outcome_schema()",
        "ctx.memory",
        "dict[str, Any]",
        "UnsupportedFeature",
        '"session"',
    ],
}
FOR_THE_SUPERVISOR = {"writes", "schedules"}
"""Methods of `Context` that the supervisor calls, not a routine."""
ATTRIBUTE = re.compile(r"self\.([a-z]\w*)\s*(?::[^=\n]+)?=")
"""A public attribute that `Context` sets, such as `self.token: str = ...`."""


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


def test_the_context_page_names_every_ctx_field() -> None:
    text = page("concepts/context-notes-memory.md")
    public = {name for name in vars(Context) if not name.startswith("_")}
    public |= set(ATTRIBUTE.findall(inspect.getsource(Context)))
    assert {"token", "memory", "note"} <= public
    for name in sorted(public - FOR_THE_SUPERVISOR):
        assert f"ctx.{name}" in text, name
