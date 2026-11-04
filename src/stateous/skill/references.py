"""The reference pages of the skill: the public pages of the documentation.

In an installed wheel, the pages are in `stateous/skill/references/`. In a checkout,
they are in `docs/`. The `:::` lines of the Python reference are mkdocstrings
directives; `render_api` replaces each one with the signature and the docstrings, so
that an agent can read the page without the docs site.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import sys
from pathlib import Path
from types import ModuleType

PAGES: tuple[str, ...] = (
    "index.md",
    "getting-started.md",
    "concepts/model.md",
    "concepts/data.md",
    "concepts/two-sides.md",
    "concepts/tasks-and-routines.md",
    "concepts/guards.md",
    "concepts/timers.md",
    "concepts/context-notes-memory.md",
    "concepts/pause-resume-cancel.md",
    "concepts/parallel.md",
    "concepts/reliability.md",
    "concepts/agents.md",
    "concepts/notices.md",
    "concepts/transports.md",
    "concepts/tenancy.md",
    "how-to/write-a-workflow.md",
    "how-to/write-a-routine.md",
    "how-to/use-an-agent.md",
    "how-to/run-supervisors.md",
    "how-to/use-postgres.md",
    "how-to/steer-tokens.md",
    "how-to/change-a-workflow.md",
    "how-to/embed-a-control-plane.md",
    "how-to/write-a-transport.md",
    "how-to/run-replicas.md",
    "inside/words.md",
    "inside/event-graph.md",
    "inside/workflows.md",
    "inside/data.md",
    "reference/cli.md",
    "reference/http.md",
    "reference/python.md",
    "reference/files.md",
    "reference/limits.md",
)
"""The public pages, relative to `docs/`, in the order of the site's menu. A fixed list,
so that a private file in `docs/` can never go into the skill."""

DIRECTIVE = re.compile(r"^::: (\S+)\s*$")
Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
Function = ast.FunctionDef | ast.AsyncFunctionDef


def reference_root() -> Path:
    """The folder that holds the pages: the wheel's copy, else the checkout's `docs/`."""
    here = Path(__file__).resolve().parent
    for root in (here / "references", here.parents[2] / "docs"):
        if (root / "index.md").is_file():
            return root
    raise FileNotFoundError("the reference pages are not in this installation of Stateous")


def render_api(text: str) -> str:
    """Replace each `::: dotted.name` line, and its indented options, with the documentation."""
    out: list[str] = []
    in_directive = False
    for line in text.splitlines(keepends=True):
        if in_directive and line.startswith((" ", "\t")) and line.strip():
            continue
        in_directive = False
        match = DIRECTIVE.match(line)
        if match is None:
            out.append(line)
            continue
        in_directive = True
        out.append(_document(match.group(1)))
    return "".join(out)


def _document(name: str) -> str:
    """The Markdown for one public name: heading, code, docstring and public members."""
    module, obj = _resolve(name)
    short = name.rsplit(".", 1)[1]
    if inspect.isclass(obj) or inspect.isfunction(obj):
        return _render(_definition(obj), f"### {name}", short)
    code, doc = _assignment(module, obj, short)
    return _section(f"### {name}", code, doc)


def _resolve(name: str) -> tuple[ModuleType, object]:
    """Import the longest module prefix, then read the attributes. ValueError if it fails."""
    parts = name.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:cut]))
        except ImportError:
            continue
        obj: object = module
        try:
            for part in parts[cut:]:
                obj = getattr(obj, part)
        except AttributeError:
            break
        return module, obj
    raise ValueError(f"{name} is not a name in this version of Stateous")


def _definition(obj: object) -> Definition:
    """The `def` or `class` node of a class or a function, from its source."""
    source = inspect.cleandoc("\n" + inspect.getsource(obj))  # pyright: ignore[reportArgumentType]
    node = ast.parse(source).body[0]
    assert isinstance(node, Definition)
    return node


def _assignment(module: ModuleType, obj: object, name: str) -> tuple[str, str]:
    """The source and the docstring of a constant or a type alias.

    The name can come from another module (a re-export), so each module of the package
    that holds the same object is searched for the assignment."""
    ours = [m for m in list(sys.modules.values()) if m.__name__.startswith("stateous")]
    for candidate in [module, *ours]:
        if candidate.__dict__.get(name) is not obj:
            continue
        source = inspect.getsource(candidate)
        body = ast.parse(source).body
        for index, node in enumerate(body):
            if _assigned_name(node) == name:
                code = ast.get_source_segment(source, node) or name
                return code.replace("  # fmt: skip", ""), _following_doc(body, index)
    raise ValueError(f"the source of {name} has no assignment of it")  # pragma: no cover


def _assigned_name(node: ast.stmt) -> str | None:
    target: ast.expr | None = None
    if isinstance(node, ast.Assign):
        target = node.targets[0]
    elif isinstance(node, ast.AnnAssign):
        target = node.target
    return target.id if isinstance(target, ast.Name) else None


def _following_doc(body: list[ast.stmt], index: int) -> str:
    """The string right after an assignment: its docstring, as mkdocstrings reads it."""
    if index + 1 < len(body):
        after = body[index + 1]
        if isinstance(after, ast.Expr) and isinstance(after.value, ast.Constant):
            value: object = after.value.value
            if isinstance(value, str):
                return inspect.cleandoc(value)
    return ""


def _render(node: Definition, heading: str, owner: str) -> str:
    if not isinstance(node, ast.ClassDef):
        return _section(heading, _signature(node), ast.get_docstring(node) or "")
    head = _decorators(node) + f"class {node.name}"
    if node.bases or node.keywords:
        head += "(" + ", ".join(ast.unparse(b) for b in [*node.bases, *node.keywords]) + ")"
    parts = [_section(heading, head, ast.get_docstring(node) or "")]
    seen: set[str] = set()
    for index, member in enumerate(node.body):
        found = _member(member, node.body, index)
        if found is None or found[0] in seen:
            continue
        seen.add(found[0])
        parts.append(_section(f"#### {owner}.{found[0]}", found[1], found[2]))
        if found[0] == "__init__" and isinstance(member, Function):
            for name, code, doc in _attributes(member):
                if name not in seen:
                    seen.add(name)
                    parts.append(_section(f"#### {owner}.{name}", code, doc))
    return "".join(parts)


def _attributes(init: Function) -> list[tuple[str, str, str]]:
    """The public attributes that `__init__` sets on `self` and documents with a string
    after the line, as mkdocstrings shows them: the name, the code and the docstring."""
    found: list[tuple[str, str, str]] = []
    for index, statement in enumerate(init.body):
        target: ast.expr | None = None
        annotation = ""
        if isinstance(statement, ast.AnnAssign):
            target, annotation = statement.target, ast.unparse(statement.annotation)
        elif isinstance(statement, ast.Assign):
            target = statement.targets[0]
        is_self = isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
        if not is_self or target is None:
            continue
        assert isinstance(target, ast.Attribute)
        doc = _following_doc(init.body, index)
        if target.attr.startswith("_") or not doc or getattr(target.value, "id", "") != "self":
            continue
        code = f"{target.attr}: {annotation}" if annotation else target.attr
        found.append((target.attr, code, doc))
    return found


def _member(member: ast.stmt, body: list[ast.stmt], index: int) -> tuple[str, str, str] | None:
    """The name, code and docstring of a public member of a class, or None."""
    if isinstance(member, Function):
        public = not member.name.startswith("_") or member.name == "__init__"
        overload = any(ast.unparse(d).endswith("overload") for d in member.decorator_list)
        if not public or overload:
            return None
        return member.name, _signature(member), ast.get_docstring(member) or ""
    name = _assigned_name(member)
    if name is None or name.startswith("_"):
        return None
    return name, ast.unparse(member), _following_doc(body, index)


def _decorators(node: Definition) -> str:
    return "".join(f"@{ast.unparse(d)}\n" for d in node.decorator_list)


def _signature(node: Function) -> str:
    kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{_decorators(node)}{kind} {node.name}({ast.unparse(node.args)}){returns}"


def _section(heading: str, code: str, doc: str) -> str:
    text = f"{heading}\n\n```python\n{code}\n```\n\n"
    return text + (f"{doc}\n\n" if doc else "")
