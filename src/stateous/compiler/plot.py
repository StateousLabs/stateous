"""The workflow as a picture: a Mermaid state diagram of the graph document.

A pure function of the document: no database, no tokens. Edges carry their event
and guard. START and END are Mermaid's own start and end points, `[*]`. A parallel
state holds its child workflows as regions (design section 8). A token's current
state can be lit. A finished token keeps the last state it was in; that state is
drawn as finished, not lit.
"""

from __future__ import annotations

from typing import Any, cast

from stateous.model.workflow import END, ERROR, JOINED, START, TIMEOUT

RESERVED = {TIMEOUT: "(timeout)", ERROR: "(error)", JOINED: "(joined)"}
"""Stateous's own events, drawn so that they read as what they are."""


def mermaid(document: dict[str, Any], *, current: str | None = None, finished: bool = False) -> str:
    """The diagram. `current`: the state to light. `finished`: the token has ended,
    so `current` is the state it ended from."""
    lines = ["stateDiagram-v2", "    direction LR"]
    children = cast("dict[str, dict[str, Any]]", document.get("workflows", {}))
    _draw(document, children, prefix="", depth=1, lines=lines)
    if current is not None:
        if finished:
            lines.append("    classDef finished fill:#e5e7eb,stroke:#4b5563,stroke-width:2px")
            lines.append(f"    class {state_id(current)} finished")
        else:
            lines.append("    classDef current fill:#fde68a,stroke:#b45309,stroke-width:2px")
            lines.append(f"    class {state_id(current)} current")
    return "\n".join(lines) + "\n"


def _draw(
    body: dict[str, Any],
    children: dict[str, dict[str, Any]],
    *,
    prefix: str,
    depth: int,
    lines: list[str],
) -> None:
    """One workflow's states and transitions. A parallel state is a composite state,
    with one region for each child workflow; `--` separates the regions."""
    pad = "    " * depth
    for name, spec in cast("dict[str, dict[str, Any]]", body["states"]).items():
        sid = prefix + state_id(name)
        if "parallel" in spec:
            lines.append(f"{pad}state {_declared(sid, name)}{{")
            for i, workflow in enumerate(cast("list[str]", spec["parallel"])):
                if i:
                    lines.append(f"{pad}    --")
                region = f"{sid}__{state_id(workflow)}"
                lines.append(f"{pad}    state {_declared(region, workflow)}{{")
                inner = region + "__"
                _draw(children[workflow], children, prefix=inner, depth=depth + 2, lines=lines)
                lines.append(f"{pad}    }}")
            lines.append(f"{pad}}}")
        elif sid != name:
            lines.append(f'{pad}state "{name}" as {sid}')
    for edge in cast("list[dict[str, Any]]", body["transitions"]):
        source, target = _point(prefix, edge["from"]), _point(prefix, edge["to"])
        event = cast("str | None", edge.get("on"))
        label = "" if event is None else RESERVED.get(event, event)
        if edge.get("when"):
            label += f" [{edge['when']}]"
        elif edge.get("otherwise"):
            label += " [otherwise]"
        label = label.strip()
        lines.append(f"{pad}{source} --> {target}" + (f": {label}" if label else ""))


def state_id(name: str) -> str:
    """A state or workflow name as a Mermaid id: hyphens become underscores."""
    return name.replace("-", "_")


def _point(prefix: str, name: str) -> str:
    """A transition's end: a state, or `[*]` for START and END."""
    return "[*]" if name in (START.name, END.name) else prefix + state_id(name)


def _declared(sid: str, name: str) -> str:
    """How a composite state or a region is declared: by its id, with its name as the
    label when the two differ. It ends in a space, before `{`."""
    return f"{sid} " if sid == name else f'"{name}" as {sid} '
