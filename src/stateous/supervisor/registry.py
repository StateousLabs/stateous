"""Finding the routines: functions marked `@on_state`.

The decorator only sets a mark. Nothing registers itself on import. The
supervisor imports every module of its folder and keeps the marked functions."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from stateous.model.contract import StateRef, implementation_of

Routine = Callable[..., Any]
"""A function marked `@on_state`: `def` or `async def`, it takes `ctx`."""


class RegistryError(ValueError):
    """The routines found do not make sense together."""


@dataclass
class Registry:
    routines: dict[str, Routine] = field(default_factory=dict[str, Routine])
    """The routine of each state, by state name."""
    states: dict[str, StateRef[Any]] = field(default_factory=dict[str, StateRef[Any]])
    """The contract's `StateRef` of each state, by state name."""

    @property
    def served(self) -> list[str]:
        """The states this registry has routines for."""
        return list(self.routines)

    @classmethod
    def from_modules(cls, modules: Iterable[ModuleType]) -> Registry:
        registry = cls()
        seen: set[int] = set()
        for module in modules:
            for value in vars(module).values():
                state = implementation_of(value) if callable(value) else None
                if state is None or id(value) in seen:
                    continue
                seen.add(id(value))
                registry._add(state, value)
        return registry

    @classmethod
    def discover(cls, folder: Path) -> Registry:
        """Import every module in `folder` (not those whose names start with `_`) and
        collect the marked functions.

        The folder stays first on the import path: routines import the generated
        `contract` and their own helpers from it while they run. A module of the
        same name already imported from another folder is forgotten first."""
        folder = folder.resolve()
        if str(folder) in sys.path:
            sys.path.remove(str(folder))
        sys.path.insert(0, str(folder))
        paths = sorted(p for p in folder.glob("*.py") if not p.stem.startswith("_"))
        for path in paths:
            loaded = sys.modules.get(path.stem)
            where = getattr(loaded, "__file__", None)
            if loaded is not None and (where is None or Path(where).resolve().parent != folder):
                del sys.modules[path.stem]
        return cls.from_modules([importlib.import_module(path.stem) for path in paths])

    def _add(self, state: StateRef[Any], routine: Routine) -> None:
        if state.name in self.routines:
            first = _where(self.routines[state.name])
            raise RegistryError(f"{state.name!r} has two routines: {first} and {_where(routine)}")
        self.routines[state.name] = routine
        self.states[state.name] = state


def _where(routine: Routine) -> str:
    name = getattr(routine, "__qualname__", None) or repr(routine)
    return f"{getattr(routine, '__module__', '?')}.{name}"
