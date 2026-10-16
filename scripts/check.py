"""Every check, in order: `uv run python scripts/check.py`.

GitHub Actions does not run for this repository, so this script is the gate
before a commit. It runs the lint rules, the format check, the types, the import
contracts, the build of the documentation site and the tests. It stops at the first
check that fails, and names it.
"""

from __future__ import annotations

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


def main() -> int:
    """Run each step; stop at the first failure. Returns the exit code."""
    for name, command in STEPS:
        print(f"== {name}", flush=True)
        if subprocess.run(command, check=False, cwd=ROOT).returncode != 0:
            print(f"{name} failed", flush=True)
            return 1
    print("all checks passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
