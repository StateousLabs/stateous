"""The guide and the README stay true to the code and to the example.

The guide shows the example files as they are. It names every command and option,
every HTTP route and every `ctx` field. Each command in the guide and the README
exists, with the options it uses. Sentences have 25 words at most (ASD-STE100).
"""

from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path

from typer.core import TyperCommand, TyperGroup, TyperOption
from typer.main import get_command

from stateous import Context
from stateous.cli.main import app

ROOT = Path(__file__).parents[2]
GUIDE = ROOT / "docs" / "guide.md"
README = ROOT / "README.md"
EXAMPLES = ROOT / "examples"
LINK = re.compile(r"\]\(([^)\s]+)\)")
PYTHON = re.compile(r"```python\n(.*?)```", re.DOTALL)
BASH = re.compile(r"```bash\n(.*?)```", re.DOTALL)
FENCE = re.compile(r"```.*?```", re.DOTALL)
EXAMPLE_FILE = re.compile(r"\A# (sdlc[\w-]*/\w+\.py)\b")
SENTENCE_END = re.compile(r"(?<=[.!?;])\s+")
LIST_ITEM = re.compile(r"\n(?=\s*(?:[-*]|\d+\.) )")
MOST_WORDS = 25
Command = TyperCommand | TyperGroup
"""Typer 0.27 has its own copy of click, so its commands are not `click` types."""
TYPER_OWN = {"--help", "--install-completion", "--show-completion"}
EXAMPLE_FILES = {
    "sdlc/flow.py",
    "sdlc/schemas.py",
    "sdlc-supervisor/testing.py",
    "sdlc-supervisor/coding.py",
}
ROUTES = [  # contract §13
    "POST /tokens",
    "GET /tokens",
    "GET /tokens/{id}",
    "GET /tokens/{id}/history",
    "POST /tokens/{id}/events/{event}",
    "POST /tokens/{id}/pause",
    "POST /tokens/{id}/resume",
    "POST /tokens/{id}/cancel",
    "POST /tasks/claim",
    "POST /tasks/{id}/report",
    "POST /tasks/heartbeat",
    "POST /tasks/{id}/note",
    "PUT /tasks/{id}/memory",
    "GET /workflow",
    "GET /health",
]
HEADINGS = [
    "## What Stateous does",
    "## The words",
    "## Two sides, two folders",
    "## Install",
    "## Write the workflow",
    "## Compile and serve",
    "## Set up a supervisor",
    "## Write a routine",
    "## Act on tokens",
    "## Agents, with PHD",
    "## Reliability",
    "## Nested and parallel work",
    "## Try it on one computer",
    "## The HTTP API",
    "## The commands",
    "## What Stateous does not do",
]
MECHANISMS = [
    "**The ledger**",
    "**Leases**",
    "**The clock**",
    "**Idempotency keys**",
    "**The routine's memory**",
    "**The supervisor's outbox**",
    "**Pause and resume**",
]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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
    """The sentences outside code blocks and tables. Each list item is its own group."""
    found: list[str] = []
    for paragraph in re.split(r"\n\s*\n", FENCE.sub("", markdown)):
        lines = [line for line in paragraph.splitlines() if not line.lstrip().startswith("|")]
        for item in LIST_ITEM.split("\n".join(lines)):
            found.extend(s for s in SENTENCE_END.split(" ".join(item.split())) if s)
    return found


def test_there_are_five_commands() -> None:
    group = cli()

    assert set(group.commands) == {"compile", "serve", "init", "supervise", "token"}
    token = child(group, "token")
    assert isinstance(token, TyperGroup)
    assert set(token.commands) == {"start", "fire", "show", "pause", "resume", "cancel"}


def test_the_guide_names_every_command_and_option() -> None:
    guide = read(GUIDE)

    for path, command in commands(cli()).items():
        assert "stateous " + " ".join(path) in guide, path
        for option in options(command):
            assert f"`{option}" in guide, (path, option)


def test_every_command_in_the_docs_exists() -> None:
    group = cli()
    for page in (GUIDE, README):
        lines = command_lines(read(page))
        assert lines, page.name
        for line in lines:
            words = shlex.split(line)[1:]
            command: Command = group
            allowed = options(group)
            while isinstance(command, TyperGroup) and words and not words[0].startswith("-"):
                name = words.pop(0)
                assert name in command.commands, f"{page.name}: {line}"
                command = child(command, name)
                allowed |= options(command)
            assert not isinstance(command, TyperGroup), f"{page.name}: {line}"
            for word in words:
                if word.startswith("--"):
                    assert word.split("=", 1)[0] in allowed, f"{page.name}: {line}: {word}"


def test_the_guide_shows_the_example_files_as_they_are() -> None:
    shown: set[str] = set()
    for block in PYTHON.findall(read(GUIDE)):
        found = EXAMPLE_FILE.match(block)
        if found is not None:
            name = found.group(1)
            assert block == read(EXAMPLES / name), f"the guide's {name} is not the file"
            shown.add(name)

    assert shown == EXAMPLE_FILES


def test_every_python_block_is_python() -> None:
    for page in (GUIDE, README):
        for number, block in enumerate(PYTHON.findall(read(page)), 1):
            try:
                ast.parse(block)
            except SyntaxError as error:
                raise AssertionError(f"{page.name}, block {number}: {error}") from None


def test_every_relative_link_reaches_a_file() -> None:
    for page in (GUIDE, README):
        for target in LINK.findall(read(page)):
            path = target.split("#", 1)[0]
            if not path or "://" in path:
                continue
            assert (page.parent / path).exists(), f"{page.name} links to {target}"


def test_the_guide_has_every_section() -> None:
    guide = read(GUIDE)

    for heading in HEADINGS:
        assert heading + "\n" in guide, heading


def test_the_guide_names_every_route() -> None:
    api = section(read(GUIDE), "## The HTTP API")

    for route in ROUTES:
        assert f"`{route}`" in api, route


def test_the_guide_names_every_ctx_field() -> None:
    routine = section(read(GUIDE), "## Write a routine")
    for_the_supervisor = {"writes", "schedules"}  # the supervisor reads these, not a routine
    public = {name for name in vars(Context) if not name.startswith("_")}
    public |= {name for name in Context.__annotations__ if not name.startswith("_")}

    for name in sorted(public - for_the_supervisor):
        assert f"ctx.{name}" in routine, name
    assert 'ctx["key"]' in routine


def test_the_guide_names_the_seven_mechanisms() -> None:
    reliability = section(read(GUIDE), "## Reliability")

    for mechanism in MECHANISMS:
        assert mechanism in reliability, mechanism


def test_the_guide_says_which_phd_features_are_not_released() -> None:
    agents = section(read(GUIDE), "## Agents, with PHD")

    for said in (
        "not released",
        "Dispatcher.run()",
        "handle.session_id",
        "idle_timeout_s",
        "event.summary",
        "start()",
        "handle.cancel()",
        "asyncio.CancelledError",
        "outcome_schema()",
        "ctx.memory",
    ):
        assert said in agents, said


def test_the_nesting_section_describes_parallel_states() -> None:
    nesting = section(read(GUIDE), "## Nested and parallel work")

    for said in (
        "parallel=[",
        'join="all"',
        'join="any"',
        "inputs=[",
        "`joined`",
        "branches.all(",
        "branches.any(",
        "`workflow`",
        "`token`",
        "`status`",
        "`final`",
        "`payload`",
        "`<parent>.<state>.<visit>.<workflow>`",
        "`<workflow>/<state>`",
        "`states.design_review.triage`",
        "409",
        "`GET /tokens?parent=<id>`",
        "otherwise=True",
        "### Work done by tokens that you start yourself",
    ):
        assert said in nesting, said


def test_the_guide_no_longer_says_that_the_engine_has_no_parallel_states() -> None:
    guide = read(GUIDE)

    assert "no nested or parallel states" not in guide
    assert "The engine has no nested states" not in guide
    assert "three events of its own: `timeout`, `error` and `joined`" in guide


def test_sentences_are_short() -> None:
    for page in (GUIDE, README):
        for sentence in sentences(read(page)):
            words = len(sentence.split())
            assert words <= MOST_WORDS, f"{page.name}: {words} words: {sentence}"


def test_the_readme_leads_to_the_guide_and_the_example() -> None:
    readme = read(README)

    for target in ("docs/guide.md", "examples/sdlc/", "examples/sdlc-supervisor/", "LICENSE"):
        assert f"]({target}" in readme, target
