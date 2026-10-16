"""Checks for every page of the site, and for the built site.

Each page is in the menu, names no private document, and has short sentences.
Each command on a page exists, with its options. Each Python block is Python, and
each Mermaid block starts with a diagram type. The built site holds no private file.
The README and the package lead to the site, and no tracked file points to the old guide.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import cast

import pytest
import yaml
from typer.core import TyperGroup

from stateous.cli.starter import flow_text

from .pages import (
    DOCS,
    MOST_WORDS,
    PRIVATE,
    PRIVATE_FILE,
    README,
    ROOT,
    Command,
    child,
    cli,
    command_lines,
    options,
    private_words,
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
OLD_GUIDE = "guide"
"""The old guide is not a page of the site."""
SITE = "https://stateouslabs.github.io/stateous/"
REPOSITORY = "https://github.com/StateousLabs/stateous"
"""PyPI shows the README, so its links are absolute: a relative link breaks there."""


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
        assert private_words(read(path)) == [], path.name


def test_no_tracked_file_names_a_private_document() -> None:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    tracked = [ROOT / name for name in listed.stdout.split("\0") if name]
    assert ROOT / "src" / "stateous" / "__init__.py" in tracked
    for path in tracked:
        if path.resolve() == PRIVATE_FILE or not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        assert private_words(text) == [], path.relative_to(ROOT).as_posix()


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The site, built once with `--strict` for the tests of this module."""
    folder = tmp_path_factory.mktemp("built") / "site"
    built = subprocess.run(
        ["uv", "run", "--group", "docs", "mkdocs", "build", "--strict", "--quiet",
         "--site-dir", str(folder)],
        cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace", timeout=300,
        check=False, env={**os.environ, "NO_MKDOCS_2_WARNING": "1"},
    )  # fmt: skip
    assert built.returncode == 0, built.stdout + built.stderr
    return folder


def test_the_python_reference_hides_how_the_supervisor_makes_ctx(site: Path) -> None:
    html = read(site / "reference" / "python" / "index.html")
    start = html.index('id="stateous.Context"')
    context = html[start : html.index('id="stateous.StateRef"', start)]

    assert "schedule" in context
    for internal in ("save_memory", "grant"):
        assert internal not in context, internal


def test_the_built_site_has_no_private_document(site: Path) -> None:
    assert (site / "index.html").is_file()
    assert (site / "reference" / "python" / "index.html").is_file()
    stems = [word.removesuffix(".md").lower() for word in PRIVATE] + [OLD_GUIDE]
    for path in site.rglob("*"):
        name = path.relative_to(site).as_posix().lower()
        assert not [stem for stem in stems if stem in name], name
        if path.suffix in {".html", ".json"}:
            assert private_words(read(path)) == [], name


ASSET_TAGS = re.compile(
    r"<link\b[^>]*\brel=[\"']?stylesheet[^>]*>|<script\b[^>]*\bsrc=[^>]*>", re.IGNORECASE
)
ASSET_URL = re.compile(r"\b(?:href|src)=[\"']([^\"']+)[\"']", re.IGNORECASE)


def test_the_built_site_has_the_theme_and_the_search(site: Path) -> None:
    assert list(site.glob("assets/stylesheets/main.*.css"))
    assert list(site.glob("assets/javascripts/bundle.*.js"))
    assert (site / "search" / "search_index.json").is_file()


def test_every_page_references_only_files_that_exist(site: Path) -> None:
    pages = list(site.rglob("*.html"))
    assert pages
    for page in pages:
        for tag in ASSET_TAGS.findall(read(page)):
            for url in ASSET_URL.findall(tag):
                if re.match(r"([a-z][a-z0-9+.-]*:|//|#)", url, re.IGNORECASE):
                    continue
                path = url.split("?", 1)[0].split("#", 1)[0]
                if path.startswith("/stateous/"):  # the 404 page: a path from the site root
                    target = site / path.removeprefix("/stateous/")
                else:
                    target = (page.parent / path).resolve()
                assert target.is_file(), f"{page.relative_to(site).as_posix()}: {url}"


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


def test_the_readme_leads_to_the_site_and_the_example() -> None:
    readme = read(README)
    assert f"]({SITE}" in readme
    for target in (
        "tree/main/examples/sdlc)",
        "tree/main/examples/sdlc-supervisor)",
        "blob/main/LICENSE",
    ):
        assert f"]({REPOSITORY}/{target}" in readme, target
        assert (ROOT / target.split("/", 2)[2].rstrip(")")).exists(), target


def test_the_readme_has_no_relative_links() -> None:
    links = re.findall(r"\]\(([^)]+)\)", read(README))
    assert links
    assert all(link.startswith("https://") for link in links), links


def test_the_package_points_to_the_site() -> None:
    assert f'Documentation = "{SITE}"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_no_tracked_file_points_to_the_guide() -> None:
    # The pattern is a regular expression, so that this file does not match it.
    found = subprocess.run(
        ["git", "grep", "-n", "docs/guide[.]md"], cwd=ROOT, capture_output=True, text=True
    )
    assert found.stdout == "", found.stdout


def test_the_tutorial_shows_the_starter_flow_as_init_writes_it() -> None:
    blocks = [
        block
        for block in PYTHON.findall(read(DOCS / "getting-started.md"))
        if block.startswith("# orders/flow.py")
    ]
    assert blocks == [flow_text("orders")]
