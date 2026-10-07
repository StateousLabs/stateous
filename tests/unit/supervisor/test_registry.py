"""Finding the routines: functions marked @on_state."""

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from stateous.model.context import Context
from stateous.model.contract import StateRef, on_state
from stateous.supervisor.registry import Registry, RegistryError

CODING = StateRef("coding")
TESTING = StateRef("testing")


def module(name: str = "routines", **functions: Any) -> ModuleType:
    made = ModuleType(name)
    for key, function in functions.items():
        setattr(made, key, function)
    return made


def test_marked_functions_are_found_by_state() -> None:
    @on_state(CODING)
    def code(ctx: Context[Any]) -> None:
        return None

    def helper() -> int:
        return 1

    registry = Registry.from_modules([module(code=code, helper=helper, VALUE=3)])

    assert registry.served == ["coding"]
    assert registry.routines["coding"] is code
    assert registry.states["coding"] is CODING


def test_two_routines_for_one_state_are_refused() -> None:
    @on_state(CODING)
    def first(ctx: Context[Any]) -> None:
        return None

    @on_state(CODING)
    def second(ctx: Context[Any]) -> None:
        return None

    with pytest.raises(RegistryError, match=r"'coding' has two routines: .*first and .*second"):
        Registry.from_modules([module("a", first=first), module("b", second=second)])


def test_a_routine_imported_into_two_modules_counts_once() -> None:
    @on_state(CODING)
    def code(ctx: Context[Any]) -> None:
        return None

    registry = Registry.from_modules([module("a", code=code), module("b", code=code)])

    assert registry.served == ["coding"]


def test_routines_are_discovered_in_a_folder(tmp_path: Path) -> None:
    (tmp_path / "contract.py").write_text(
        "from stateous import StateRef\nTESTING = StateRef('testing')\n", encoding="utf-8"
    )
    (tmp_path / "testing.py").write_text(
        "from contract import TESTING\nfrom stateous import on_state\n\n\n"
        "@on_state(TESTING)\ndef test(ctx: object) -> None:\n    return None\n",
        encoding="utf-8",
    )
    (tmp_path / "_private.py").write_text("raise SystemExit('never imported')\n", encoding="utf-8")

    registry = Registry.discover(tmp_path)

    assert registry.served == ["testing"]


def test_two_folders_with_modules_of_the_same_name_are_each_discovered(tmp_path: Path) -> None:
    for folder, state in (("one", "coding"), ("two", "testing")):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "contract.py").write_text(
            f"from stateous import StateRef\nSTATE = StateRef('{state}')\n", encoding="utf-8"
        )
        (tmp_path / folder / "routine.py").write_text(
            "from contract import STATE\nfrom stateous import on_state\n\n\n"
            "@on_state(STATE)\ndef work(ctx: object) -> None:\n    return None\n",
            encoding="utf-8",
        )

    first = Registry.discover(tmp_path / "one")
    second = Registry.discover(tmp_path / "two")

    assert (first.served, second.served) == (["coding"], ["testing"])


def test_a_child_states_routine_is_served_by_its_claim_name() -> None:
    triage = StateRef("security/triage")

    @on_state(triage)
    def review(ctx: Context[Any]) -> None:
        return None

    registry = Registry.from_modules([module(review=review)])

    assert registry.served == ["security/triage"]
    assert registry.states["security/triage"] is triage
