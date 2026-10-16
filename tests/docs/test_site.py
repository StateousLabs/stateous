"""Checks for every page of the site, and for the built site.

Each page is in the menu, names no private document, and has short sentences.
Each command on a page exists, with its options. Each Python block is Python, and
each Mermaid block starts with a diagram type. The built site holds no private file.
"""

from __future__ import annotations

import ast
import re
import shlex
import subprocess
from pathlib import Path
from typing import cast

import yaml
from typer.core import TyperGroup

from .pages import (
    DOCS,
    MOST_WORDS,
    PRIVATE,
    README,
    ROOT,
    Command,
    child,
    cli,
    command_lines,
    options,
    read,
    sentences,
    site_pages,
)

MERMAID = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)
PYTHON = re.compile(r"```python\n(.*?)```", re.DOTALL)
DIAGRAM_TYPES = (
    "flowchart",
    "graph",
    "sequenceDiagram",
    "stateDiagram-v2",
    "erDiagram",
    "classDiagram",
)
BUILT_PRIVATE = ("design", "phd", "PHD_ANSWERS", "superpowers", "guide")
"""Parts of file names that the built site must not have."""


def menu_pages() -> set[str]:
    """Every page that the `nav` of mkdocs.yml names."""
    config = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    config = config.replace("!!python/name:", "")  # the tag of the Mermaid fence format
    nav = cast(list[object], yaml.safe_load(config)["nav"])
    found: set[str] = set()

    def walk(items: list[object]) -> None:
        for item in items:
            if isinstance(item, str):
                found.add(item)
            elif isinstance(item, dict):
                for value in cast(dict[str, object], item).values():
                    walk(cast(list[object], value) if isinstance(value, list) else [value])

    walk(nav)
    return found


def test_every_page_is_in_the_menu() -> None:
    pages = {path.relative_to(DOCS).as_posix() for path in site_pages()}

    assert pages == menu_pages()


def test_no_page_names_a_private_document() -> None:
    for path in [*site_pages(), README]:
        text = read(path)
        for word in PRIVATE:
            assert word not in text, f"{path.name} names {word!r}"


def test_the_built_site_has_no_private_document(tmp_path: Path) -> None:
    site = tmp_path / "site"
    built = subprocess.run(
        ["uv", "run", "--group", "docs", "mkdocs", "build", "--strict", "--quiet",
         "--site-dir", str(site)],
        cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace", timeout=300,
        check=False,
    )  # fmt: skip

    assert built.returncode == 0, built.stdout + built.stderr
    assert (site / "index.html").is_file()
    for name in BUILT_PRIVATE:
        assert not list(site.rglob(f"*{name}*")), name


def test_sentences_are_short() -> None:
    for path in [*site_pages(), README]:
        for sentence in sentences(read(path)):
            words = len(sentence.split())
            assert words <= MOST_WORDS, f"{path.name}: {words} words: {sentence}"


def test_every_command_on_the_site_exists() -> None:
    group = cli()
    for path in [*site_pages(), README]:
        for line in command_lines(read(path)):
            words = shlex.split(line)[1:]
            command: Command = group
            allowed = options(group)
            while isinstance(command, TyperGroup) and words and not words[0].startswith("-"):
                name = words.pop(0)
                assert name in command.commands, f"{path.name}: {line}"
                command = child(command, name)
                allowed |= options(command)
            assert not isinstance(command, TyperGroup), f"{path.name}: {line}"
            for word in words:
                if word.startswith("--"):
                    assert word.split("=", 1)[0] in allowed, f"{path.name}: {line}: {word}"


def test_every_python_block_is_python() -> None:
    for path in [*site_pages(), README]:
        for number, block in enumerate(PYTHON.findall(read(path)), 1):
            try:
                ast.parse(block)
            except SyntaxError as error:
                raise AssertionError(f"{path.name}, block {number}: {error}") from None


def test_every_mermaid_block_has_a_diagram_type() -> None:
    for path in site_pages():
        for block in MERMAID.findall(read(path)):
            assert block.lstrip().startswith(DIAGRAM_TYPES), f"{path.name}: {block[:40]}"
