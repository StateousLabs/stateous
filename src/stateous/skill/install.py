"""Write the skill for a coding agent into a folder that the developer chooses."""

from __future__ import annotations

import shutil
import tempfile
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

from stateous.skill.references import PAGES, reference_root, render_api

SKILL_NAME = "stateous"
"""The name of the skill, and of its folder."""
PYTHON_REFERENCE = "reference/python.md"
TEMPLATES = ("SKILL.md", "scripts/try_workflow.py")
"""The files of the skill that hold `{version}`."""


class SkillExists(Exception):
    """The folder of the skill exists, and `force` is not set."""


def install_skill(directory: Path, *, force: bool = False) -> Path:
    """Write `directory/stateous/` and return its path.

    The skill is first written to a new folder next to it, then moved into place. So a
    failed write leaves the old skill as it was. Raises `SkillExists` when the folder
    exists and `force` is not set, and `OSError` when a write fails."""
    target = directory / SKILL_NAME
    if target.exists() and not force:
        raise SkillExists(f"{target} exists; add --force to replace it")
    directory.mkdir(parents=True, exist_ok=True)
    new = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}-", dir=directory))
    try:
        _write(new)
        if target.exists():
            shutil.rmtree(target)
        new.rename(target)
    finally:
        shutil.rmtree(new, ignore_errors=True)
    return target


def _write(folder: Path) -> None:
    current = version("stateous")
    package = files("stateous.skill")
    for name in TEMPLATES:
        text = package.joinpath(name).read_text(encoding="utf-8")
        _put(folder / name, text.replace("{version}", current))
    root = reference_root()
    for page in PAGES:
        text = (root / page).read_text(encoding="utf-8")
        if page == PYTHON_REFERENCE:
            text = render_api(text)
        _put(folder / "references" / page, text)


def _put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
