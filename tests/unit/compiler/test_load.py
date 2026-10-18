"""Loading the one workflow of a flow.py."""

import sys
from pathlib import Path

import pytest

from stateous.compiler.load import LoadError, load_workflow
from stateous.model.workflow import DefinitionError

SCHEMAS = """\
from pydantic import BaseModel


class Ticket(BaseModel):
    repo: str
"""

FLOW = """\
from schemas import Ticket

from stateous import END, START, Workflow

flow = Workflow("sdlc", context=Ticket)
coding = flow.state("coding")
flow.transition(START, coding)
flow.transition(coding, END, on=flow.event("done"))
"""


def project(tmp_path: Path, flow: str = FLOW, schemas: str = SCHEMAS) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "schemas.py").write_text(schemas, encoding="utf-8")
    path = tmp_path / "flow.py"
    path.write_text(flow, encoding="utf-8")
    return path


def test_the_one_workflow_is_found_and_its_siblings_import(tmp_path: Path) -> None:
    flow = load_workflow(project(tmp_path))

    assert flow.name == "sdlc"
    assert flow.context is not None and flow.context.__name__ == "Ticket"


def test_the_modules_it_imported_are_forgotten(tmp_path: Path) -> None:
    before = set(sys.modules)

    load_workflow(project(tmp_path))

    assert "schemas" not in sys.modules
    assert "flow" not in sys.modules
    assert str(tmp_path) not in sys.path
    assert set(sys.modules) - before == set()


def test_two_projects_never_share_their_modules(tmp_path: Path) -> None:
    first = load_workflow(project(tmp_path / "a"))
    other = SCHEMAS + "    title: str\n"
    second = load_workflow(project(tmp_path / "b", schemas=other))

    assert first.context is not None and "title" not in first.context.model_fields
    assert second.context is not None and "title" in second.context.model_fields


def test_a_file_without_a_workflow_is_refused(tmp_path: Path) -> None:
    path = project(tmp_path, flow="x = 1\n")

    with pytest.raises(LoadError, match="defines no Workflow at module level"):
        load_workflow(path)


def test_a_file_with_two_workflows_is_refused(tmp_path: Path) -> None:
    flow = "from stateous import Workflow\n\na = Workflow('a')\nb = Workflow('b')\n"

    with pytest.raises(LoadError, match=r"defines 2 workflows \('a', 'b'\); keep one per file"):
        load_workflow(project(tmp_path, flow=flow))


def test_one_workflow_under_two_names_is_one(tmp_path: Path) -> None:
    flow = FLOW + "alias = flow\n"

    assert load_workflow(project(tmp_path, flow=flow)).name == "sdlc"


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="no such file"):
        load_workflow(tmp_path / "nothing.py")


def test_a_syntax_error_names_the_line(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="line 2"):
        load_workflow(project(tmp_path, flow="x = 1\nflow = (\n"))


def test_an_import_that_fails_says_why(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="ModuleNotFoundError: No module named 'nothing'"):
        load_workflow(project(tmp_path, flow="import nothing\n"))


def test_a_definition_error_stays_a_definition_error(tmp_path: Path) -> None:
    flow = "from stateous import Workflow\n\nflow = Workflow('sdlc')\nflow.event('timeout')\n"

    with pytest.raises(DefinitionError, match="event 'timeout' is Stateous's own"):
        load_workflow(project(tmp_path, flow=flow))


FAMILY = """\
from schemas import Ticket

from stateous import END, START, Workflow

review = Workflow("review", context=Ticket)
waiting = review.state("waiting")
review.transition(START, waiting)
review.transition(waiting, END, on=review.event("lgtm"))

flow = Workflow("sdlc", context=Ticket)
reviews = flow.state("reviews", parallel=[review], inputs=["repo"])
flow.transition(START, reviews)
flow.transition(reviews, END, on="joined")
"""


def test_the_workflow_that_no_parallel_state_runs_is_the_one(tmp_path: Path) -> None:
    flow = load_workflow(project(tmp_path, flow=FAMILY))

    assert flow.name == "sdlc"
    assert [child.name for child in flow.states["reviews"].parallel] == ["review"]


def test_two_top_workflows_beside_a_child_are_refused(tmp_path: Path) -> None:
    flow = FAMILY + "\nother = Workflow('other')\n"

    with pytest.raises(
        LoadError, match=r"defines 2 workflows \('sdlc', 'other'\); keep one per file"
    ):
        load_workflow(project(tmp_path, flow=flow))


def test_a_file_that_python_cannot_import_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "flow.txt"
    path.write_text("flow = 1\n", encoding="utf-8")

    with pytest.raises(LoadError, match=r"flow.txt: cannot be imported"):
        load_workflow(path)
