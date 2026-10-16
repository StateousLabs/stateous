"""The inside pages: every internal workflow has its five elements, and the event graph
names them."""

from __future__ import annotations

import re

import pytest

from stateous.control.store.tables import Base

from .pages import page, section

WORKFLOWS = [
    "Compile",
    "Serve",
    "Start a token",
    "Claim a task",
    "Heartbeat",
    "Report an outcome",
    "Lease expiry",
    "Timer fires",
    "Pause and resume",
    "Cancel",
    "Join of a parallel state",
    "Supervisor start",
]
ELEMENTS = ["**Trigger**", "**Steps**", "**Actors**", "**Events**", "**Data**"]
TABLES = [
    "ledger",
    "tokens",
    "tasks",
    "timers",
    "memory",
    "claims",
    "control_plane",
    "stateous_version",
]


@pytest.mark.parametrize("name", WORKFLOWS)
def test_each_workflow_has_its_five_elements(name: str) -> None:
    card = section(page("inside/workflows.md"), f"## {name}")
    for element in ELEMENTS:
        assert element in card, (name, element)


def test_the_event_graph_names_every_workflow() -> None:
    text = page("inside/event-graph.md")
    assert "```mermaid" in text
    for name in WORKFLOWS:
        assert name in text, name


def test_the_data_page_names_every_table_and_the_outbox() -> None:
    text = page("inside/data.md")
    for table in TABLES:
        assert f"`{table}`" in text, table
    assert "outbox.db" in text
    assert "```mermaid" in text


ENTITY = re.compile(r"^    (\w+) \{\n(.*?)^    \}", re.MULTILINE | re.DOTALL)
"""An entity of the erDiagram on `inside/data.md`, with its column lines."""


def test_the_data_diagram_has_the_columns_of_each_table() -> None:
    shown = {
        name: [line.split()[1] for line in body.strip().splitlines()]
        for name, body in ENTITY.findall(page("inside/data.md"))
    }
    real = {name: list(table.columns.keys()) for name, table in Base.metadata.tables.items()}

    assert set(real) <= set(shown), set(real) - set(shown)
    for name, columns in real.items():
        assert sorted(shown[name]) == sorted(columns), name
