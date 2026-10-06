"""The example: its committed files are current, its two folders hold what the
design says (section 3), and its `testing` routine does what the guide says.

If a test of a committed file fails after a change to the compiler or the code
generator, write the files again. First run `uv run stateous compile
examples/sdlc/flow.py`. Then write examples/sdlc-worker/contract.py with
`generate_contract(load_graph(text))`, where `text` is the new graph file.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from stateous.model.graph_file import load_graph

ROOT = Path(__file__).parents[2]
FLOW = ROOT / "examples" / "sdlc"
GRAPH = FLOW / "build" / "sdlc.json"
STATEOUS = [sys.executable, "-m", "stateous"]


def test_the_committed_graph_file_is_current(tmp_path: Path) -> None:
    out = tmp_path / "sdlc.json"
    done = subprocess.run(
        [*STATEOUS, "compile", str(FLOW / "flow.py"), "--out", str(out)],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert out.read_text(encoding="utf-8") == GRAPH.read_text(encoding="utf-8")


def test_the_graph_is_the_workflow_of_the_design() -> None:
    graph = load_graph(GRAPH.read_text(encoding="utf-8"))

    assert (graph["stateous"], graph["workflow"], graph["context"]) == (1, "sdlc", "Ticket")
    assert graph["states"] == {
        "coding": {"timeout": "6h", "retries": 2, "emits": ["done", "stuck"]},
        "testing": {"timeout": "1h", "emits": ["passed", "failed"]},
    }
    assert graph["events"] == {
        "done": {},
        "stuck": {},
        "passed": {"payload": "Tests"},
        "failed": {"payload": "Tests"},
    }
    assert graph["transitions"] == [
        {"from": "__start__", "to": "coding"},
        {"from": "coding", "to": "testing", "on": "done"},
        {"from": "coding", "to": "__end__", "on": "stuck"},
        {"from": "testing", "to": "__end__", "on": "passed"},
        {"from": "testing", "to": "coding", "on": "failed", "when": "count('failed') < 3"},
        {"from": "testing", "to": "__end__", "on": "failed", "otherwise": True},
    ]
    assert set(graph["schemas"]) == {"Ticket", "Tests"}
