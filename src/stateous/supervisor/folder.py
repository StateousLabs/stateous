"""A supervisor's folder: `stateous.yaml`, the generated `contract.py`, the routines.

`stateous init` writes it (`write_folder`). `stateous supervise` reads it
(`load_folder`). The folder never holds the graph: `contract.py` is its only
knowledge of the workflow."""

from __future__ import annotations

import ast
import json
import re
import socket
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

CONFIG = "stateous.yaml"
CONTRACT = "contract.py"
DEFAULT_TASKS = 4
MAX_TASKS = 1000
"""The most routines at once: the API takes at most 1000 tasks in one heartbeat."""

TAKEN_NAMES = frozenset({"contract", "stateous", *sys.stdlib_module_names})
"""Module names that a stub file must not have: a file of the folder with such a
name would hide that module (the folder is first on the import path)."""

_CHECKSUM = re.compile(r'^__checksum__ = "(sha256:[0-9a-f]{64})"$', re.MULTILINE)


class FolderError(Exception):
    """A supervisor's folder that cannot be used. The message is one plain sentence."""


class FolderConfig(BaseModel):
    """`stateous.yaml`."""

    model_config = ConfigDict(extra="forbid")

    url: str
    """The control plane."""
    tasks: int = Field(default=DEFAULT_TASKS, ge=1, le=MAX_TASKS)
    """How many routines run at once."""
    name: str | None = Field(default=None, min_length=1, max_length=128)
    """The supervisor's name in the token's history. Default: the host name."""

    @field_validator("url")
    @classmethod
    def _http(cls, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"url: {url!r} is not an http:// or https:// address")
        try:
            parsed = httpx2.URL(url)
        except (httpx2.InvalidURL, ValueError) as error:
            said = str(error)
            raise ValueError(
                f"url: {url!r} is not a valid address: {said[:1].lower()}{said[1:]}"
            ) from None
        if not parsed.host:
            raise ValueError(f"url: {url!r} has no host")
        if parsed.port is not None and not 0 < parsed.port <= 65535:
            raise ValueError(f"url: {url!r} has the port {parsed.port}; a port is 1 to 65535")
        return url.rstrip("/")

    def supervisor_name(self) -> str:
        return self.name or socket.gethostname()


@dataclass(frozen=True)
class Folder:
    """A supervisor's folder, loaded."""

    path: Path
    config: FolderConfig
    checksum: str
    """The checksum of the graph that `contract.py` was generated from."""


def load_folder(path: Path) -> Folder:
    """Read the folder's `stateous.yaml` and the checksum in its `contract.py`.
    The routines are not imported here."""
    if not path.is_dir():
        raise FolderError(f"{path} is not a folder")
    contract, config = path / CONTRACT, path / CONFIG
    for needed in (contract, config):
        if not needed.is_file():
            raise FolderError(f"{path} has no {needed.name}: run `stateous init URL {path}`")
    try:
        text = contract.read_text(encoding="utf-8")
        data: Any = yaml.safe_load(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise FolderError(f"cannot read {path}: {_one_line(str(error))}") from None
    found = _CHECKSUM.search(text)
    if found is None:
        raise FolderError(f"{contract} has no __checksum__: run `stateous init URL {path}` again")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise FolderError(f"cannot use {config}: it is not a mapping of settings")
    try:
        settings = FolderConfig.model_validate(data)
    except ValidationError as error:
        raise FolderError(f"cannot use {config}: {plain_problems(error)}") from None
    return Folder(path.resolve(), settings, found.group(1))


def plain_problems(error: ValidationError) -> str:
    """What is wrong with a settings file, in plain words on one line: each setting
    named as it is written, and what is wrong with it. No pydantic types or links."""
    said: list[str] = []
    for problem in error.errors():
        where = ".".join(str(part) for part in problem["loc"])
        if problem["type"] == "value_error":
            said.append(problem["msg"].removeprefix("Value error, "))
        elif problem["type"] == "extra_forbidden":
            said.append(f"{where}: there is no such setting")
        elif problem["type"] == "missing":
            said.append(f"{where}: missing")
        else:
            said.append(f"{where}: {problem['msg'][:1].lower()}{problem['msg'][1:]}")
    return "; ".join(said)


def stub_name(state: str) -> str:
    """The file a state's stub goes in: `<state>.py`, or `<state>_routine.py` when
    `<state>` is the name of a module that the file would hide (`TAKEN_NAMES`)."""
    name = _identifier(state)
    if name in TAKEN_NAMES:
        return f"{name}_routine.py"
    return f"{name}.py"


def implemented_states(folder: Path) -> set[str]:
    """The states (as Python identifiers) that a file in `folder` marks with
    `@on_state(states.<state>)`. The files are parsed, not imported. A file that
    does not parse is not counted."""
    found: set[str] = set()
    for path in sorted(folder.glob("*.py")):
        if path.name == CONTRACT:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for decorator in node.decorator_list:
                    state = _marked_state(decorator)
                    if state is not None:
                        found.add(state)
    return found


def write_folder(
    folder: Path,
    *,
    url: str,
    contract: str,
    states: Sequence[str],
    stub: Callable[[str], str],
) -> list[str]:
    """Write `contract.py` (always), a stub for each state that no file implements
    (never over an existing file), and `stateous.yaml` (only when absent). Returns
    one line for each file: `wrote`, `kept` or `skipped`."""
    folder.mkdir(parents=True, exist_ok=True)
    done: list[str] = []
    target = folder / CONTRACT
    target.write_text(contract, encoding="utf-8", newline="\n")
    done.append(f"wrote    {target}")
    implemented = implemented_states(folder)
    for state in states:
        path = folder / stub_name(state)
        if _identifier(state) in implemented:
            done.append(f"kept     the routine of {state!r}")
        elif path.exists():
            done.append(f"skipped  {path}: the file exists and has no routine for {state!r}")
        else:
            path.write_text(stub(state), encoding="utf-8", newline="\n")
            why = ""
            if _identifier(state) in TAKEN_NAMES:
                why = f" ({_identifier(state)}.py would hide a Python module)"
            done.append(f"wrote    {path}{why}")
    config = folder / CONFIG
    if config.exists():
        done.append(f"kept     {config}")
    else:
        config.write_text(_config_text(url), encoding="utf-8", newline="\n")
        done.append(f"wrote    {config}")
    return done


def _config_text(url: str) -> str:
    return (
        "# A Stateous supervisor. Run it with: stateous supervise <this folder>\n"
        f"url: {json.dumps(url)}    # the control plane\n"
        f"tasks: {DEFAULT_TASKS}    # how many routines run at once\n"
        "# name: build-box-1    # optional; default: the host name\n"
    )


def _identifier(state: str) -> str:
    return state.replace("-", "_")


def _marked_state(decorator: ast.expr) -> str | None:
    """`x` for a decorator `on_state(states.x)` or `stateous.on_state(contract.states.x)`."""
    if not isinstance(decorator, ast.Call) or len(decorator.args) != 1:
        return None
    called = decorator.func
    if isinstance(called, ast.Name):
        name = called.id
    elif isinstance(called, ast.Attribute):
        name = called.attr
    else:
        return None
    argument = decorator.args[0]
    if name != "on_state" or not isinstance(argument, ast.Attribute):
        return None
    owner = argument.value
    if (isinstance(owner, ast.Name) and owner.id == "states") or (
        isinstance(owner, ast.Attribute) and owner.attr == "states"
    ):
        return argument.attr
    return None


def _one_line(text: str) -> str:
    return " ".join(text.split())
