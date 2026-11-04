"""`SKILL.md` stays true: each command, option and page that it names exists, and it
lists every reference page. Its front matter fits the Agent Skills format."""

from __future__ import annotations

import re
import shlex
from importlib.resources import files
from typing import cast

import yaml
from typer.core import TyperGroup

from stateous.skill.references import PAGES
from stateous.tryout.main import parser

from ...docs.pages import (
    MOST_WORDS,
    Command,
    child,
    cli,
    command_lines,
    commands,
    options,
    private_words,
    sentences,
)

FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
INLINE_COMMAND = re.compile(r"`(stateous [^`]+)`")
OPTION = re.compile(r"(?<![\w-])(--[a-z][a-z-]*)")
PAGE = re.compile(r"references/([\w./-]+\.md)")


def skill_md() -> str:
    return files("stateous.skill").joinpath("SKILL.md").read_text(encoding="utf-8")


def front() -> dict[str, str]:
    match = FRONT.match(skill_md())
    assert match is not None
    data: object = yaml.safe_load(match.group(1))
    assert isinstance(data, dict)
    return {str(key): str(value) for key, value in cast("dict[object, object]", data).items()}


def body() -> str:
    return FRONT.sub("", skill_md(), count=1)


def stateous_lines() -> list[str]:
    inline = [line.split(" #", 1)[0].strip() for line in INLINE_COMMAND.findall(body())]
    return [*command_lines(body()), *inline]


def test_the_front_matter_fits_the_format() -> None:
    data = front()
    assert data["name"] == "stateous"
    assert 0 < len(data["description"]) <= 1024
    assert set(data) == {"name", "description"}


def test_the_body_names_the_version() -> None:
    assert "Stateous {version}." in body()


def test_every_command_in_skill_md_exists() -> None:
    group = cli()
    found = stateous_lines()
    assert found
    for line in found:
        words = [word for word in shlex.split(line)[1:] if not word.isupper()]
        command: Command = group
        while isinstance(command, TyperGroup) and words and not words[0].startswith("-"):
            name = words.pop(0)
            assert name in command.commands, line
            command = child(command, name)
        assert not isinstance(command, TyperGroup), line


def test_every_option_in_skill_md_exists() -> None:
    known: set[str] = set()
    for command in commands(cli()).values():
        known |= options(command)
    for action in parser()._actions:  # pyright: ignore[reportPrivateUsage]
        known.update(action.option_strings)
    for option in OPTION.findall(body()):
        assert option in known, option


def test_every_page_in_skill_md_is_a_reference_page() -> None:
    named = PAGE.findall(body())
    assert named
    assert set(named) <= set(PAGES)


def test_every_reference_page_is_listed() -> None:
    assert set(PAGES) <= set(PAGE.findall(body()))


def test_skill_md_names_no_private_document() -> None:
    assert private_words(skill_md()) == []


def test_sentences_are_short() -> None:
    for sentence in sentences(body()):
        assert len(sentence.split()) <= MOST_WORDS, sentence
