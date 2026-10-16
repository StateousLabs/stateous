"""`stateous compile FLOW [--out FILE]`: it writes the graph file, or names the error."""

import json
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from stateous.cli.main import app

SCHEMAS = """\
from pydantic import BaseModel


class Ticket(BaseModel):
    repo: str


class Tests(BaseModel):
    passed: int
    failed: int
"""

FLOW = """\
from schemas import Tests, Ticket

from stateous import END, START, Workflow

flow = Workflow("sdlc", context=Ticket)

coding = flow.state("coding", timeout="6h", retries=2)
testing = flow.state("testing", timeout="1h")

done = flow.event("done")
stuck = flow.event("stuck")
passed = flow.event("passed", payload=Tests)
failed = flow.event("failed", payload=Tests)

flow.transition(START, coding)
flow.transition(coding, testing, on=done)
flow.transition(coding, END, on=stuck)
flow.transition(testing, END, on=passed)
flow.transition(testing, coding, on=failed, when="count('failed') < 3")
flow.transition(testing, END, on=failed, otherwise=True)
"""

runner = CliRunner()


def project(tmp_path: Path, flow: str = FLOW, schemas: str = SCHEMAS) -> Path:
    folder = tmp_path / "sdlc"
    folder.mkdir()
    (folder / "schemas.py").write_text(schemas, encoding="utf-8")
    path = folder / "flow.py"
    path.write_text(flow, encoding="utf-8")
    return path


def test_compile_writes_the_graph_file_next_to_the_flow(tmp_path: Path) -> None:
    flow = project(tmp_path)

    result = runner.invoke(app, ["compile", str(flow)])

    assert result.exit_code == 0, result.output
    graph = flow.parent / "build" / "sdlc.json"
    text = graph.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    document = json.loads(text)
    assert document["workflow"] == "sdlc"
    assert document["states"]["coding"] == {
        "timeout": "6h",
        "retries": 2,
        "emits": ["done", "stuck"],
    }
    assert f"wrote {graph}" in result.output


def test_out_names_another_file(tmp_path: Path) -> None:
    flow = project(tmp_path)
    out = tmp_path / "elsewhere" / "graph.json"

    result = runner.invoke(app, ["compile", str(flow), "--out", str(out)])

    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text(encoding="utf-8"))["workflow"] == "sdlc"
    assert not (flow.parent / "build").exists()


def refused(tmp_path: Path, flow: str) -> str:
    result = runner.invoke(app, ["compile", str(project(tmp_path, flow))])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert not (tmp_path / "sdlc" / "build").exists()
    return result.output


def test_a_workflow_that_is_not_valid_is_refused_with_every_problem(tmp_path: Path) -> None:
    output = refused(tmp_path, FLOW + 'flow.state("orphan")\n')

    assert "error: workflow 'sdlc' is not valid:" in output
    assert "state 'orphan' cannot be reached from START" in output


def test_a_mistake_made_while_declaring_is_refused(tmp_path: Path) -> None:
    assert "event 'timeout' is Stateous's own" in refused(
        tmp_path, FLOW + 'flow.event("timeout")\n'
    )


def test_a_name_that_cannot_be_python_is_refused_now(tmp_path: Path) -> None:
    flow = FLOW.replace('"testing"', '"pass"')

    assert "'pass' cannot be used as a Python name" in refused(tmp_path, flow)


def test_a_file_that_does_not_load_is_refused(tmp_path: Path) -> None:
    assert "defines no Workflow at module level" in refused(tmp_path, "x = 1\n")
    assert "no such file" in runner.invoke(app, ["compile", str(tmp_path / "none.py")]).output


LATER = """\
from __future__ import annotations

from pydantic import BaseModel


class Ticket(BaseModel):
    repo: str
    tests: Tests | None = None


class Tests(BaseModel):
    passed: int
    failed: int
"""


def test_a_model_that_names_a_later_model_compiles(tmp_path: Path) -> None:
    flow = project(tmp_path, schemas=LATER)

    result = runner.invoke(app, ["compile", str(flow)])

    assert result.exit_code == 0, result.output
    document = json.loads((flow.parent / "build" / "sdlc.json").read_text(encoding="utf-8"))
    assert document["schemas"]["Ticket"]["properties"]["tests"]["anyOf"][0] == {
        "$ref": "#/schemas/Tests"
    }


def one_problem(output: str, sentence: str) -> None:
    """The output is the error's heading and one line: the problem."""
    lines = output.strip().splitlines()
    assert lines[0] == "error: workflow 'sdlc' is not valid:", output
    assert len(lines) == 2, output
    assert sentence in lines[1], output


def test_a_model_that_names_a_model_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    schemas = LATER.replace("tests: Tests | None", "tests: Missing | None")

    result = runner.invoke(app, ["compile", str(project(tmp_path, schemas=schemas))])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    one_problem(result.output, "the model Ticket cannot be written as JSON Schema")


NO_SCHEMA = """\
from pydantic import BaseModel, ConfigDict


class Pipe:
    pass


class Ticket(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    repo: str
    pipe: Pipe | None = None


class Tests(BaseModel):
    passed: int
    failed: int
"""


def test_a_model_without_a_json_schema_is_refused(tmp_path: Path) -> None:
    result = runner.invoke(app, ["compile", str(project(tmp_path, schemas=NO_SCHEMA))])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    one_problem(result.output, "the model Ticket cannot be written as JSON Schema")


def test_python_dash_m_stateous_runs_the_same_command(tmp_path: Path) -> None:
    flow = project(tmp_path)

    result = subprocess.run(
        [sys.executable, "-m", "stateous", "compile", str(flow)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (flow.parent / "build" / "sdlc.json").exists()
