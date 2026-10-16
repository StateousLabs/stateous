"""Shared helpers for the tests of the documentation site.

The page list, the commands of the CLI, the sentences and the sections of a page.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

from typer.core import TyperCommand, TyperGroup, TyperOption
from typer.main import get_command

from stateous.cli.main import app

ROOT = Path(__file__).parents[2]
DOCS = ROOT / "docs"
README = ROOT / "README.md"
# The private words are encoded (base64 of a newline-joined list), so that this public
# test does not name the private files. Decode the value to read the list.
_PRIVATE_ENCODED = (
    "ZGVzaWduLm1kCnBoZC5tZApQSERfQU5TV0VSUwpORVhULm1kCnN1cGVycG93ZXJzCnN0YXRlb3VzLWZsb3cK"
    "Y29udHJhY3Qgc2VjdGlvbgpidWlsZCBwbGFuCmRlc2lnbiBzZWN0aW9u"
)
PRIVATE: tuple[str, ...] = tuple(base64.b64decode(_PRIVATE_ENCODED).decode("utf-8").splitlines())
"""Words that no published or tracked file names, and files under docs/ that are not pages."""
PRIVATE_FILE = Path(__file__).resolve()
"""The one tracked file that holds the private words."""
BASH = re.compile(r"```bash\n(.*?)```", re.DOTALL)
FENCE = re.compile(r"```.*?```", re.DOTALL)
SENTENCE_END = re.compile(r"(?<=[.!?;])\s+")
LIST_ITEM = re.compile(r"\n(?=\s*(?:[-*]|\d+\.) )")
MOST_WORDS = 25
Command = TyperCommand | TyperGroup
"""Typer 0.27 has its own copy of click, so its commands are not `click` types."""
TYPER_OWN = {"--help", "--install-completion", "--show-completion"}


def site_pages() -> list[Path]:
    """Every page of the site: each .md under docs/, without the private files."""
    found: list[Path] = []
    for path in sorted(DOCS.rglob("*.md")):
        relative = path.relative_to(DOCS).as_posix()
        if any(word.lower() in relative.lower() for word in PRIVATE):
            continue
        found.append(path)
    return found


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def private_words(text: str) -> list[str]:
    """The private words in a text, without regard to case."""
    lowered = text.lower()
    return [word for word in PRIVATE if word.lower() in lowered]


def page(relative: str) -> str:
    """The text of a page, by its path under docs/."""
    return read(DOCS / relative)


def cli() -> TyperGroup:
    command = get_command(app)
    assert isinstance(command, TyperGroup)
    return command


def child(group: TyperGroup, name: str) -> Command:
    command = group.commands[name]
    assert isinstance(command, TyperCommand | TyperGroup), name
    return command


def options(command: Command) -> set[str]:
    found: set[str] = set()
    for parameter in command.params:
        if isinstance(parameter, TyperOption):
            found.update(parameter.opts, parameter.secondary_opts)
    return found - TYPER_OWN


def commands(group: TyperGroup, path: tuple[str, ...] = ()) -> dict[tuple[str, ...], Command]:
    found: dict[tuple[str, ...], Command] = {}
    for name in group.commands:
        command = child(group, name)
        found[(*path, name)] = command
        if isinstance(command, TyperGroup):
            found.update(commands(command, (*path, name)))
    return found


def command_lines(markdown: str) -> list[str]:
    """Each `stateous …` line of the bash blocks, without its comment."""
    found: list[str] = []
    for block in BASH.findall(markdown):
        for line in block.splitlines():
            command = line.split(" #", 1)[0].strip()
            if command.startswith("stateous "):
                found.append(command)
    return found


def section(markdown: str, heading: str) -> str:
    start = markdown.index(heading + "\n")
    end = markdown.find("\n## ", start + len(heading))
    return markdown[start : end if end != -1 else len(markdown)]


def sentences(markdown: str) -> list[str]:
    """The sentences outside code blocks and tables. Each list item is its own group.

    A Mermaid block is a code block, so its lines are not sentences."""
    found: list[str] = []
    for paragraph in re.split(r"\n\s*\n", FENCE.sub("", markdown)):
        lines = [line for line in paragraph.splitlines() if not line.lstrip().startswith("|")]
        for item in LIST_ITEM.split("\n".join(lines)):
            found.extend(s for s in SENTENCE_END.split(" ".join(item.split())) if s)
    return found
