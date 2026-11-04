"""The try-out of the skill on the example's `coding` routine, which runs an agent with PHD.

tests/examples/fake_phd.py takes the place of `phd.Dispatcher`, so no agent starts. All
else is real: the example's graph and routine, the control plane and the supervisor.
The folder has no routine of `testing`, so the try-out stops there.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import phd
import pytest

from stateous.tryout.run import try_workflow

from .fake_phd import FakeDispatcher, Script

ROOT = Path(__file__).parents[2]
GRAPH = ROOT / "examples" / "sdlc" / "build" / "sdlc.json"
EXAMPLE = ROOT / "examples" / "sdlc-supervisor"
MODULES = ("contract", "coding")


def fake_credentials(harness: str) -> phd.Credentials:
    return phd.Credentials.claude_oauth_token("test-token")


@pytest.fixture
def folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """A copy of the example's folder with only `coding`, in a project with the stack
    `stacks/dev` and the repository `acme/api`. The fake PHD is in place."""
    saved = {name: sys.modules.pop(name) for name in MODULES if name in sys.modules}
    copy = tmp_path / "sdlc-supervisor"
    copy.mkdir()
    for name in ("coding.py", "contract.py", "stateous.yaml"):
        shutil.copy(EXAMPLE / name, copy / name)
    stack = tmp_path / "stacks" / "dev"
    (stack / "agents").mkdir(parents=True)
    (stack / "stack.json").write_text(json.dumps({"name": "dev", "version": "0.1.0"}), "utf-8")
    (stack / "agents" / "dev.md").write_text("You write code.\n", encoding="utf-8")
    (tmp_path / "acme" / "api").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phd, "Dispatcher", FakeDispatcher)
    monkeypatch.setattr(phd.Credentials, "from_host", fake_credentials)
    monkeypatch.setattr(FakeDispatcher, "script", Script())
    monkeypatch.setattr(FakeDispatcher, "calls", [])
    yield copy
    for name in MODULES:
        sys.modules.pop(name, None)
    sys.modules.update(saved)


def test_the_try_out_runs_the_phd_routine(folder: Path) -> None:
    report = asyncio.run(
        try_workflow(
            GRAPH,
            folder,
            {"repo": "acme/api"},
            timeout=60,
            quiet=30,
            database=folder.parent / "try.db",
        )
    )

    coding = report.steps[0]
    assert coding.state == "coding"
    assert coding.outcome == "event done"
    assert coding.notes == ["wrote the code"]
    assert len(FakeDispatcher.calls) == 1
    assert report.stop.reason == "no_routine"
    assert report.stop.state == "testing"
