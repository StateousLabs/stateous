"""`install_skill`: write the skill folder, refuse an existing one, replace it with force."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

import pytest

from stateous.skill.install import SKILL_NAME, SkillExists, install_skill
from stateous.skill.references import PAGES


def test_install_writes_the_skill(tmp_path: Path) -> None:
    path = install_skill(tmp_path / "skills")
    assert path == tmp_path / "skills" / SKILL_NAME
    skill_md = (path / "SKILL.md").read_text(encoding="utf-8")
    assert skill_md.startswith("---\nname: stateous\n")
    assert version("stateous") in skill_md
    assert "{version}" not in skill_md
    script = (path / "scripts" / "try_workflow.py").read_text(encoding="utf-8")
    assert f'SKILL_VERSION = "{version("stateous")}"' in script
    assert all((path / "references" / page).is_file() for page in PAGES)
    python = (path / "references" / "reference" / "python.md").read_text(encoding="utf-8")
    assert ":::" not in python


def test_install_leaves_no_other_folder(tmp_path: Path) -> None:
    install_skill(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == [SKILL_NAME]


def test_install_refuses_an_existing_skill(tmp_path: Path) -> None:
    install_skill(tmp_path)
    with pytest.raises(SkillExists, match="--force"):
        install_skill(tmp_path)


def test_force_replaces_the_skill(tmp_path: Path) -> None:
    path = install_skill(tmp_path)
    (path / "old.txt").write_text("x", encoding="utf-8")
    install_skill(tmp_path, force=True)
    assert not (path / "old.txt").exists()
    assert (path / "SKILL.md").is_file()


def test_a_failed_write_leaves_the_old_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = install_skill(tmp_path)
    (path / "old.txt").write_text("x", encoding="utf-8")

    def broken(text: str) -> str:
        raise OSError("disk full")

    monkeypatch.setattr("stateous.skill.install.render_api", broken)
    with pytest.raises(OSError):
        install_skill(tmp_path, force=True)
    assert (path / "old.txt").is_file()
    assert [p.name for p in tmp_path.iterdir()] == [SKILL_NAME]
