"""Loading a workflow from its `flow.py`, for `stateous compile`.

The file's folder is on the import path while it loads, so `flow.py` can import
its sibling `schemas.py`. The modules it imports are forgotten afterwards, so two
projects with a `flow.py` each can be compiled in one process. No bytecode is
written: a cached `.pyc` can outlive an edit made in the same second.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from stateous.model.workflow import DefinitionError, Workflow


class LoadError(ValueError):
    """A `flow.py` cannot be loaded, or does not hold exactly one workflow."""


def load_workflow(path: Path) -> Workflow:
    """The one `Workflow` at module level in the file at `path`.

    Raises:
        LoadError: The file is missing, does not import, or holds zero or two
            workflows.
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
    if len(found) > 1:
        names = ", ".join(repr(flow.name) for flow in found)
        raise LoadError(f"{path} defines {len(found)} workflows ({names}); keep one per file")
    return found[0]


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
