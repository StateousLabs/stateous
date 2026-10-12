"""The pyproject.toml that `stateous init` writes (see the guide, "Set up a supervisor").

The supervisor's folder is a Python project of its own. It depends on Stateous and
on what its routines import, so `uv sync` makes its environment (`.venv`), and
editors find `stateous` there. Until Stateous is published, `--stateous-path`
points the dependency at a local checkout, as an editable `[tool.uv.sources]` entry.

The text is built in Python, as `stateous.compiler.codegen` builds `contract.py`."""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

from stateous.cli.errors import CommandError

PACKAGE = "stateous"
PYPROJECT = "pyproject.toml"
DEFAULT_NAME = "supervisor"
"""The project's name when nothing of the folder's name can be one."""

_HEADER = """\
# Written by `stateous init`. This folder is a Python project of its own:
# `uv sync` makes its environment (.venv), and `uv run stateous ...` runs in it.
# Add what your routines import to `dependencies`.
"""

_SOURCE_COMMENT = "# A local checkout of Stateous, until it is published: delete this table then."


def checkout(path: Path) -> str:
    """`path` as a Stateous checkout: absolute, with forward slashes. Raises
    `CommandError` when `path` is not a folder that holds a pyproject.toml with the
    project name `stateous`."""
    resolved = path.expanduser().resolve()
    data: dict[str, Any]
    try:
        data = tomllib.loads((resolved / PYPROJECT).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        data = {}
    project: Any = data.get("project")
    name: object = None
    if isinstance(project, dict):
        table = cast("dict[str, object]", project)
        name = table.get("name")
    if name != PACKAGE:
        raise CommandError(
            f"--stateous-path {path} is not a Stateous checkout: give the folder that holds "
            f"its {PYPROJECT}"
        )
    return resolved.as_posix()


def write_pyproject(folder: Path, *, stateous_path: str | None = None) -> str:
    """Write `folder/pyproject.toml`, only when it is absent. `stateous_path` is a
    checkout, as `checkout` returns it. Returns one line: `wrote    <path>` or
    `kept     <path>`."""
    target = folder / PYPROJECT
    if target.exists():
        return f"kept     {target}"
    folder.mkdir(parents=True, exist_ok=True)
    text = pyproject_text(project_name(folder.resolve().name), stateous_path)
    target.write_text(text, encoding="utf-8", newline="\n")
    return f"wrote    {target}"


def pyproject_text(name: str, stateous_path: str | None) -> str:
    """The text of the file: a `[project]`, and a `[tool.uv.sources]` table when a
    checkout is given."""
    lines = [
        _HEADER,
        "[project]",
        f"name = {_toml_string(name)}",
        'version = "0.1.0"',
        'requires-python = ">=3.12"',
        f"dependencies = [{_toml_string(requirement())}]",
    ]
    if stateous_path is not None:
        lines += [
            "",
            _SOURCE_COMMENT,
            "[tool.uv.sources]",
            f"{PACKAGE} = {{ path = {_toml_string(stateous_path)}, editable = true }}",
        ]
    return "\n".join(lines) + "\n"


def requirement() -> str:
    """`stateous>=<the version that runs>`: `contract.py` is written by this version,
    and an older Stateous may not read it. Only `stateous` when the version is not
    known."""
    try:
        return f"{PACKAGE}>={version(PACKAGE)}"
    except PackageNotFoundError:
        return PACKAGE


def project_name(folder_name: str) -> str:
    """`folder_name` as a valid project name: letters, digits, `.`, `_` and `-`, with
    a letter or a digit at the start and at the end."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", folder_name).strip("._-")
    return cleaned or DEFAULT_NAME


def _toml_string(text: str) -> str:
    """`text` as a TOML basic string."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = re.sub(r"[\x00-\x1f\x7f]", lambda found: f"\\u{ord(found.group()):04x}", escaped)
    return f'"{escaped}"'
