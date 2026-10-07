"""Loading a workflow from its `flow.py`, for `stateous compile`.

The file's folder is on the import path while it loads, so `flow.py` can import
its sibling `schemas.py`. The modules it imports are forgotten afterwards, so two
projects with a `flow.py` each can be compiled in one process. Before that, each
model in them is completed: a model that names a model defined later in its file
(`from __future__ import annotations`) needs its module to resolve the name.
The file holds one workflow, and maybe the child workflows that its parallel
states run. It is an error when it holds zero workflows, or two that no parallel
state runs.

No bytecode is written: a cached `.pyc` can outlive an edit made in the same
second.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from pydantic import BaseModel

from stateous.model.workflow import DefinitionError, Workflow


class LoadError(ValueError):
    """A `flow.py` cannot be loaded, or does not hold one workflow that no parallel
    state runs."""


def load_workflow(path: Path) -> Workflow:
    """The one `Workflow` at module level in the file at `path` that no parallel
    state runs.

    Raises:
        LoadError: The file is missing, does not import, or holds zero workflows,
            or two that no parallel state runs.
        DefinitionError: The file makes a mistake while it declares the workflow.
    """
    file = path.resolve()
    if not file.is_file():
        raise LoadError(f"{path}: no such file")
    folder = str(file.parent)
    before = set(sys.modules)
    writes_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, folder)
    try:
        module = _run(file)
        _complete_models([sys.modules[name] for name in set(sys.modules) - before])
    finally:
        sys.path.remove(folder)
        sys.path_importer_cache.pop(folder, None)
        sys.dont_write_bytecode = writes_bytecode
        for name in set(sys.modules) - before:
            del sys.modules[name]
    workflows = [value for value in vars(module).values() if isinstance(value, Workflow)]
    found = list({id(flow): flow for flow in workflows}.values())  # one object, one workflow
    if not found:
        raise LoadError(f"{path} defines no Workflow at module level")
    # The child workflows of parallel states are at module level too (design section 8).
    # The workflow that no parallel state runs is the one to compile.
    run = {id(child) for flow in found for s in flow.states.values() for child in s.parallel}
    tops = [flow for flow in found if id(flow) not in run] or found
    if len(tops) > 1:
        names = ", ".join(repr(flow.name) for flow in tops)
        raise LoadError(f"{path} defines {len(tops)} workflows ({names}); keep one per file")
    return tops[0]


def _complete_models(modules: list[ModuleType]) -> None:
    """Build each model that pydantic could not build yet, while its module can
    still be found. A name that stays unknown is reported when compiling."""
    for module in modules:
        for value in list(vars(module).values()):
            mine = isinstance(value, type) and value.__module__ == module.__name__
            if mine and issubclass(value, BaseModel) and not value.__pydantic_complete__:
                value.model_rebuild(raise_errors=False)


def _run(file: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(file.stem, file)
    if spec is None or spec.loader is None:
        raise LoadError(f"{file}: cannot be imported")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except DefinitionError:
        raise
    except SyntaxError as error:
        raise LoadError(f"{file}, line {error.lineno}: {error.msg}") from None
    except Exception as error:
        raise LoadError(f"{file} does not import: {type(error).__name__}: {error}") from None
    return module
