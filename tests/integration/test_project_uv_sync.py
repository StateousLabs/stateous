"""`uv sync` in a folder with the pyproject.toml that `stateous init` writes. uv gets
the packages from the package index unless its cache has them: skip this test
offline with `-m 'not network'`."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from stateous.cli.project_file import checkout, write_pyproject

CHECKOUT = Path(__file__).resolve().parents[2]
"""This repository: a Stateous checkout."""

UV = shutil.which("uv")


@pytest.mark.network
@pytest.mark.skipif(UV is None, reason="uv is not on the PATH")
def test_uv_sync_makes_an_environment_with_stateous_from_the_checkout(tmp_path: Path) -> None:
    assert UV is not None
    folder = tmp_path / "sdlc-supervisor"
    write_pyproject(folder, stateous_path=checkout(CHECKOUT))
    env = {
        k: v for k, v in os.environ.items() if k not in {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}
    }

    synced = subprocess.run(
        [UV, "sync"], cwd=folder, env=env, capture_output=True, text=True, timeout=600, check=False
    )

    assert synced.returncode == 0, synced.stdout + synced.stderr
    scripts = "Scripts" if sys.platform == "win32" else "bin"
    python = folder / ".venv" / scripts / ("python.exe" if sys.platform == "win32" else "python")
    found = subprocess.run(
        [str(python), "-c", "import stateous, pathlib; print(pathlib.Path(stateous.__file__))"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert found.returncode == 0, found.stderr
    assert Path(found.stdout.strip()).is_relative_to(CHECKOUT / "src")
