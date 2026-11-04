"""The reference pages of the skill: the public docs, with the Python reference filled in."""

from __future__ import annotations

import subprocess
import tomllib
import zipfile
from pathlib import Path
from typing import cast

import pytest
import yaml

from stateous.skill.references import PAGES, reference_root, render_api

from ...docs.pages import PRIVATE, ROOT


def nav_pages() -> list[str]:
    """The pages that the `nav` of mkdocs.yml names, in its order."""
    config = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    config = config.replace("!!python/name:", "")  # the tag of the Mermaid fence format
    found: list[str] = []

    def walk(items: list[object]) -> None:
        for item in items:
            if isinstance(item, str):
                found.append(item)
            elif isinstance(item, dict):
                for value in cast(dict[str, object], item).values():
                    walk(cast(list[object], value) if isinstance(value, list) else [value])

    walk(cast(list[object], yaml.safe_load(config)["nav"]))
    return found


def test_the_pages_are_the_menu_of_the_site() -> None:
    assert list(PAGES) == nav_pages()


def test_every_page_is_found() -> None:
    root = reference_root()
    assert all((root / page).is_file() for page in PAGES)


def test_no_page_names_a_private_document() -> None:
    for page in PAGES:
        text = (reference_root() / page).read_text(encoding="utf-8").lower()
        assert not [word for word in PRIVATE if word.lower() in text], page


def test_render_api_fills_a_class() -> None:
    out = render_api("Intro.\n\n::: stateous.ControlPlane\n\nEnd.\n")
    assert ":::" not in out
    assert out.startswith("Intro.\n")
    assert out.endswith("End.\n")
    assert "### stateous.ControlPlane" in out
    assert "def start(" in out
    assert "The token. Raises `TokenNotFound`" in out
    assert "_scope" not in out


def test_render_api_fills_a_function_and_a_constant() -> None:
    out = render_api("::: stateous.supervise\n::: stateous.ALL_SCOPES\n")
    assert "async def supervise(" in out
    assert "ALL_SCOPES = " in out
    assert "act across every scope" in out


def test_render_api_drops_the_options_of_a_directive() -> None:
    out = render_api("::: stateous.Context\n    options:\n      show_attribute_values: false\n")
    assert "options:" not in out
    assert "### stateous.Context" in out


def test_render_api_shows_the_fields_of_a_dataclass() -> None:
    out = render_api("::: stateous.Token\n")
    assert "#### Token.status" in out


def test_render_api_refuses_a_name_that_does_not_exist() -> None:
    with pytest.raises(ValueError, match=r"stateous\.Nope"):
        render_api("::: stateous.Nope\n")


def test_the_rendered_python_reference_has_no_directive() -> None:
    text = render_api((reference_root() / "reference/python.md").read_text(encoding="utf-8"))
    assert ":::" not in text


def test_the_wheel_packs_each_page_by_name() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packed = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert packed == {f"docs/{page}": f"stateous/skill/references/{page}" for page in PAGES}


@pytest.mark.e2e
def test_the_wheel_holds_the_pages_and_no_other(tmp_path: Path) -> None:
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path)], cwd=ROOT, check=True)
    (wheel,) = tmp_path.glob("*.whl")
    prefix = "stateous/skill/references/"
    with zipfile.ZipFile(wheel) as archive:
        names = [n for n in archive.namelist() if n.startswith(prefix)]
    assert sorted(n.removeprefix(prefix) for n in names) == sorted(PAGES)
