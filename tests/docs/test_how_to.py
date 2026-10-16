"""Each how-to page holds the steps and facts it must hold, and shows the example files as
they are."""

from __future__ import annotations

import re

import pytest

from .pages import ROOT, page

EXAMPLE_FILE = re.compile(r"\A# (sdlc[\w-]*/\w+\.py)\b")
PYTHON = re.compile(r"```python\n(.*?)```", re.DOTALL)

FACTS: dict[str, list[str]] = {
    "how-to/write-a-workflow.md": [
        "stateous compile",
        "Workflow(",
        "flow.state(",
        "flow.event(",
        "flow.transition(",
        "payload=",
        "timeout=",
        "retries=",
    ],
    "how-to/write-a-routine.md": [
        "@on_state(",
        "async def",
        "-> events.",
        "return None",
        "raise",
        "states.",
        "outcome(",
    ],
    "how-to/run-supervisors.md": [
        "stateous.yaml",
        "stateous supervise",
        "Ctrl-C",
        "second",
        "stateous serve",
        "--supervise",
        "uv sync",
    ],
    "how-to/use-postgres.md": [
        "--db",
        "postgresql://",
        "one control plane",
        "statement_timeout",
        "lock_timeout",
    ],
    "how-to/steer-tokens.md": [
        "stateous token start",
        "stateous token fire",
        "stateous token show",
        "--key",
        "Client(",
        "Idempotency-Key",
    ],
    "how-to/change-a-workflow.md": [
        "stateous compile",
        "stateous serve",
        "live tokens",
        "stateous init supervisor",
    ],
}


@pytest.mark.parametrize("name", sorted(FACTS))
def test_the_page_holds_its_facts(name: str) -> None:
    text = page(name)
    for fact in FACTS[name]:
        assert fact in text, (name, fact)


def test_the_example_files_are_shown_as_they_are() -> None:
    shown: set[str] = set()
    for name in FACTS:
        for block in PYTHON.findall(page(name)):
            found = EXAMPLE_FILE.match(block)
            if found is not None:
                example = ROOT / "examples" / found.group(1)
                assert block == example.read_text(encoding="utf-8"), found.group(1)
                shown.add(found.group(1))
    assert shown == {"sdlc/flow.py", "sdlc/schemas.py", "sdlc-supervisor/testing.py"}
