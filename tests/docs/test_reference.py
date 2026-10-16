"""The reference pages list exactly what the code has: the commands and their options,
the routes and the refusal codes. Each file and each limit has its place."""

from __future__ import annotations

import inspect
import re

from typer.core import TyperGroup

import stateous
import stateous.client
from stateous.control import engine

from .pages import ROOT, cli, commands, options, page, section

HEADING = re.compile(r"^### stateous ([\w -]+)$", re.MULTILINE)
ROUTE = re.compile(r"`(GET|POST|PUT|DELETE) (/[^`]*)`")
CODE_ROW = re.compile(r"^\| `([a-z_]+)` \| \d{3}", re.MULTILINE)
"""A row of the table of refusal codes: the code, then its status. A row of a table of
fields has no status."""
LITERAL_CODE = re.compile(
    r'(?:refusal|Problem|_answer)\(\s*\d+,[^\n]*?"([a-z_]+)"\s*\)'
    r'|code="([a-z_]+)"|"code": "([a-z_]+)"'
)
"""A code that the API or `serve` writes as text: in `refusal(...)`, `Problem(...)` and
`_answer(...)`, as `code=` and as a JSON key."""
API_ROUTE = re.compile(r'Route\("([^"]+)", [^\n]*methods=\["([A-Z]+)"\]\)')
PATH_PART = re.compile(r"\{\w+(:\w+)?\}")
LISTING = "GET /tokens?parent=<id>"
"""The one route form on the page that is a query, not a path of its own."""


def real_routes() -> set[str]:
    """Each route of the API as `METHOD /path`, with `{id}` for a token or a task."""
    source = (ROOT / "src/stateous/control/api.py").read_text(encoding="utf-8")
    found: set[str] = set()
    for path, method in API_ROUTE.findall(source):
        shown = PATH_PART.sub("{id}", path).replace("{id}/events/{id}", "{id}/events/{event}")
        found.add(f"{method} {shown}")
    return found


def subclasses(cls: type[engine.EngineError]) -> set[type[engine.EngineError]]:
    """Every subclass of `cls`, at every depth."""
    found: set[type[engine.EngineError]] = set(cls.__subclasses__())
    for sub in list(found):
        found |= subclasses(sub)
    return found


def real_codes() -> set[str]:
    """Each refusal code: those of the engine's errors, and those that the API and
    `serve` write as text."""
    codes: set[str] = {cls.code for cls in subclasses(engine.EngineError)}
    codes.add(engine.EngineError.code)
    for name in ("src/stateous/control/api.py", "src/stateous/cli/serving.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for groups in LITERAL_CODE.findall(text):
            codes |= {code for code in groups if code}
    return codes


def test_the_cli_reference_has_exactly_the_commands() -> None:
    real = {
        " ".join(path)
        for path, command in commands(cli()).items()
        if not isinstance(command, TyperGroup)
    }

    assert set(HEADING.findall(page("reference/cli.md"))) == real


def test_the_cli_reference_names_every_option_in_its_command() -> None:
    text = page("reference/cli.md").replace("### ", "## ")
    for path, command in commands(cli()).items():
        if isinstance(command, TyperGroup):
            continue
        part = section(text, "## stateous " + " ".join(path))
        for option in options(command):
            assert f"`{option}" in part, (path, option)


def test_the_http_reference_has_exactly_the_routes() -> None:
    shown = {f"{method} {path}" for method, path in ROUTE.findall(page("reference/http.md"))}

    assert real_routes() <= shown
    assert shown - {LISTING} <= real_routes()


def test_the_http_reference_has_exactly_the_codes() -> None:
    assert set(CODE_ROW.findall(page("reference/http.md"))) == real_codes()


def test_the_files_page_shows_each_file() -> None:
    text = page("reference/files.md")
    for said in (
        "stateous.yaml",
        "url:",
        "contract.py",
        "pyproject.toml",
        "build/",
        ".json",
        "--stateous-path",
        ".stateous/outbox.db",
    ):
        assert said in text, said


def test_the_limits_page_has_both_parts() -> None:
    text = page("reference/limits.md")

    assert "## What Stateous does not do" in text
    assert "## Known limits" in text


def test_the_python_reference_covers_every_public_name() -> None:
    text = page("reference/python.md")
    for name in stateous.__all__:
        assert f"::: stateous.{name}\n" in text, name
    for name in stateous.client.__all__:
        if name != "Client":
            assert f"::: stateous.client.{name}\n" in text, name


def test_every_public_name_has_a_docstring() -> None:
    for name in stateous.__all__:
        value: object = getattr(stateous, name)
        if not (inspect.isclass(value) or inspect.isfunction(value)):
            continue
        assert inspect.getdoc(value), name
        members: dict[str, object] = dict(vars(value)) if inspect.isclass(value) else {}
        for member_name, member in members.items():
            if not member_name.startswith("_") and inspect.isfunction(member):
                assert inspect.getdoc(member), f"{name}.{member_name}"
