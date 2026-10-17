"""Every check, in order: `uv run python scripts/check.py`.

Run it before a commit. CI (.github/workflows/ci.yml) runs it too. It runs the lint
rules, the format check, the types, the import contracts, the build of the
documentation site and the tests. It stops at the first
check that fails, and names it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STEPS: list[tuple[str, list[str]]] = [
    ("lint", ["uv", "run", "ruff", "check", "."]),
    ("format", ["uv", "run", "ruff", "format", "--check", "."]),
    ("types", ["uv", "run", "pyright"]),
    ("imports", ["uv", "run", "lint-imports"]),
    ("docs", ["uv", "run", "--group", "docs", "mkdocs", "build", "--strict", "--quiet"]),
    ("tests", ["uv", "run", "pytest"]),
]
QUIET: dict[str, dict[str, str]] = {"docs": {"NO_MKDOCS_2_WARNING": "1"}}
"""Extra environment for a step. Material prints a large notice about MkDocs 2.0 without it."""


def main() -> int:
    """Run each step; stop at the first failure. Returns the exit code."""
    for name, command in STEPS:
        print(f"== {name}", flush=True)
        env = {**os.environ, **QUIET.get(name, {})}
        if subprocess.run(command, check=False, cwd=ROOT, env=env).returncode != 0:
            print(f"{name} failed", flush=True)
            return 1
    print("all checks passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
