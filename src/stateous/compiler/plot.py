"""The workflow as a picture: a Mermaid state diagram of the graph document.

A pure function of the document: no database, no tokens. Edges carry their event
and guard. START and END are Mermaid's own start and end points, `[*]`. A token's
current state can be lit. A finished token keeps the last state it was in; that
state is drawn as finished, not lit.
"""

from __future__ import annotations

from typing import Any, cast

from stateous.model.workflow import END, ERROR, START, TIMEOUT

RESERVED = {TIMEOUT: "(timeout)", ERROR: "(error)"}
"""Stateous's own events, drawn so that they read as what they are."""


def mermaid(document: dict[str, Any], *, current: str | None = None, finished: bool = False) -> str:
    """The diagram. `current`: the state to light. `finished`: the token has ended,
    so `current` is the state it ended from."""
    lines = ["stateDiagram-v2", "    direction LR"]
    for name in cast("dict[str, Any]", document["states"]):
        sid = state_id(name)
        if sid != name:
            lines.append(f'    state "{name}" as {sid}')
    for edge in cast("list[dict[str, Any]]", document["transitions"]):
        source, target = _point(edge["from"]), _point(edge["to"])
        event = cast("str | None", edge.get("on"))
        label = "" if event is None else RESERVED.get(event, event)
        if edge.get("when"):
            label += f" [{edge['when']}]"
        elif edge.get("otherwise"):
            label += " [otherwise]"
        label = label.strip()
        lines.append(f"    {source} --> {target}" + (f": {label}" if label else ""))
    if current is not None:
        if finished:
            lines.append("    classDef finished fill:#e5e7eb,stroke:#4b5563,stroke-width:2px")
            lines.append(f"    class {state_id(current)} finished")
        else:
            lines.append("    classDef current fill:#fde68a,stroke:#b45309,stroke-width:2px")
            lines.append(f"    class {state_id(current)} current")
    return "\n".join(lines) + "\n"


def state_id(name: str) -> str:
    """A state name as a Mermaid id: hyphens become underscores."""
    return name.replace("-", "_")


def _point(name: str) -> str:
    """A transition's end: a state, or `[*]` for START and END."""
    return "[*]" if name in (START.name, END.name) else state_id(name)
