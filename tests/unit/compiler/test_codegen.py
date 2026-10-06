"""The graph document -> contract.py and a stub per state (contract section 7)."""

import importlib.util
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from stateous import END, START, OutcomeError, StateRef, Workflow
from stateous.compiler.codegen import CodegenError, generate_contract, generate_stub
from stateous.compiler.compile import compile_workflow
from stateous.model.contract import implementation_of
from stateous.model.graph_file import checksum

EXPECTED = """\
# GENERATED from the `sdlc` workflow — do not edit.
# <checksum>
from __future__ import annotations

from pydantic import BaseModel

from stateous import Context, Occurrence, StateRef

__workflow__ = "sdlc"
__checksum__ = "<checksum>"


class Tests(BaseModel):
    passed: int
    failed: int


class Ticket(BaseModel):
    repo: str


TicketContext = Context[Ticket]


class events:
    class done(Occurrence[None]):
        name = "done"

    class stuck(Occurrence[None]):
        name = "stuck"

    class passed(Occurrence[Tests]):
        name = "passed"

    class failed(Occurrence[Tests]):
        name = "failed"


class states:
    coding = StateRef("coding", emits=(events.done, events.stuck))
    testing = StateRef("testing", emits=(events.passed, events.failed))
"""


@pytest.fixture(autouse=True)
def forget_generated_modules() -> Iterator[None]:
    yield
    for name in ("contract", "stub"):
        sys.modules.pop(name, None)


def load(tmp_path: Path, name: str, source: str) -> ModuleType:
    """Write `source` as `<name>.py` and import it as `name`."""
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def ruff_formatted(path: Path) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "ruff", "format", "--isolated", "--line-length", "100"]
    return subprocess.run(
        [*command, "--check", str(path)], capture_output=True, text=True, check=False
    )


def pyright_errors(folder: Path) -> list[str]:
    """Strict pyright on the files in `folder`: one `file:line: message` per error."""
    config = {"typeCheckingMode": "strict", "pythonVersion": "3.11"}
    (folder / "pyrightconfig.json").write_text(json.dumps(config), encoding="utf-8")
    command = [sys.executable, "-m", "pyright", "--outputjson", "--pythonpath", sys.executable]
    result = subprocess.run(
        [*command, "--project", str(folder)],
        capture_output=True,
        text=True,
        check=False,
        cwd=folder,
    )
    report: dict[str, Any] = json.loads(result.stdout)
    return [
        f"{Path(d['file']).name}:{d['range']['start']['line'] + 1}: {d['message']}"
        for d in report["generalDiagnostics"]
        if d["severity"] == "error"
    ]


# ── contract.py ───────────────────────────────────────────────────────────


def test_the_designs_example_gives_the_contracts_file(sdlc_document: dict[str, Any]) -> None:
    source = generate_contract(sdlc_document)

    assert source == EXPECTED.replace("<checksum>", checksum(sdlc_document))


def test_the_file_is_formatted_as_ruff_formats_it(
    tmp_path: Path, sdlc_document: dict[str, Any]
) -> None:
    path = tmp_path / "contract.py"
    path.write_text(generate_contract(sdlc_document), encoding="utf-8")

    result = ruff_formatted(path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_contract_imports_and_works(tmp_path: Path, sdlc_document: dict[str, Any]) -> None:
    contract = load(tmp_path, "contract", generate_contract(sdlc_document))

    assert contract.__workflow__ == "sdlc"
    assert contract.__checksum__ == checksum(sdlc_document)
    assert contract.TicketContext.__args__ == (contract.Ticket,)
    report = contract.Tests(passed=3, failed=0)
    happened: Any = contract.events.passed(report)
    assert (happened.name, happened.payload) == ("passed", report)
    coding: StateRef[Any] = contract.states.coding
    assert coding.name == "coding"
    assert coding.emits == (contract.events.done, contract.events.stuck)
    answer = {"event": "failed", "payload": {"passed": 1, "failed": 2}}
    assert type(contract.states.testing.outcome(answer)) is contract.events.failed


CODING = """\
from typing import Any

from stateous import on_state

from contract import TicketContext, events, states


@on_state(states.coding)
async def code(ctx: TicketContext) -> events.done | events.stuck:
    answer: Any = {"event": "done"}
    return states.coding.outcome(answer)
"""

WRONG = """\
from typing import Any

from stateous import on_state

from contract import TicketContext, events, states


@on_state(states.testing)
async def test(ctx: TicketContext) -> events.passed:
    answer: Any = {"event": "passed", "payload": {"passed": 1, "failed": 0}}
    return states.testing.outcome(answer)
"""


def test_strict_pyright_checks_what_a_routine_returns(
    tmp_path: Path, sdlc_document: dict[str, Any]
) -> None:
    (tmp_path / "contract.py").write_text(generate_contract(sdlc_document), encoding="utf-8")
    (tmp_path / "coding.py").write_text(CODING, encoding="utf-8")
    (tmp_path / "wrong.py").write_text(WRONG, encoding="utf-8")

    errors = pyright_errors(tmp_path)

    # The contract and design section 4.2's routine are clean. A routine typed
    # `-> events.passed` cannot return what `testing` may emit, `passed | failed`.
    assert len(errors) == 1, errors
    assert errors[0].startswith("wrong.py:11: ")
    assert "failed" in errors[0]


class Ticket(BaseModel):
    repo: str
    pr_number: int | None = None
    labels: list[str] = []
    meta: dict[str, Any] = {}


class Notes(BaseModel):
    verdict: Literal["approve", "reject"]


class Empty(BaseModel):
    pass


def odd_workflow() -> Workflow:
    flow = Workflow("review", context=Ticket)
    waiting = flow.state("awaiting-review")
    closing = flow.state("closing", timeout="1h")
    flow.transition(START, waiting)
    flow.transition(waiting, closing, on=flow.event("lgtm", payload=Notes))
    flow.transition(waiting, waiting, on=flow.event("changes-requested", payload=Empty))
    flow.transition(closing, END, on="timeout")
    return flow


def test_types_are_rebuilt_from_the_schemas(tmp_path: Path) -> None:
    source = generate_contract(compile_workflow(odd_workflow()))
    contract = load(tmp_path, "contract", source)

    assert "from typing import Any, Literal" in source
    ticket = contract.Ticket(repo="acme/api")
    assert (ticket.pr_number, ticket.labels, ticket.meta) == (None, [], {})
    assert contract.Notes(verdict="approve").verdict == "approve"
    with pytest.raises(ValueError, match="verdict"):
        contract.Notes(verdict="maybe")
    assert contract.Empty().model_dump() == {}


def test_names_that_are_not_python_names_keep_their_real_names(tmp_path: Path) -> None:
    contract = load(tmp_path, "contract", generate_contract(compile_workflow(odd_workflow())))

    assert contract.states.awaiting_review.name == "awaiting-review"
    assert contract.events.changes_requested.name == "changes-requested"
    assert contract.states.closing.emits == ()


def test_a_state_with_no_events_is_typed_never(tmp_path: Path) -> None:
    source = generate_contract(compile_workflow(odd_workflow()))
    contract = load(tmp_path, "contract", source)

    assert "from typing import Any, Literal, Never\n" in source
    assert '    closing = StateRef[Never]("closing", emits=())\n' in source
    assert "    awaiting_review = StateRef(" in source
    with pytest.raises(OutcomeError, match="'closing' may emit no event"):
        contract.states.closing.outcome({"event": "lgtm"})


def test_without_a_context_model_there_is_no_alias_and_no_unused_import(tmp_path: Path) -> None:
    flow = Workflow("plain")
    a = flow.state("a")
    flow.transition(START, a)
    flow.transition(a, END, on=flow.event("done"))

    source = generate_contract(compile_workflow(flow))
    contract = load(tmp_path, "contract", source)

    assert "from stateous import Occurrence, StateRef\n" in source
    assert "pydantic" not in source
    assert "Context" not in source
    assert contract.states.a.emits == (contract.events.done,)


# ── names that cannot be Python ───────────────────────────────────────────


def single(name: str) -> dict[str, Any]:
    flow = Workflow("w")
    a = flow.state(name)
    flow.transition(START, a)
    flow.transition(a, END, on=flow.event("done"))
    return compile_workflow(flow)


def test_a_keyword_cannot_be_a_state_name() -> None:
    with pytest.raises(CodegenError, match="'pass' cannot be used as a Python name"):
        generate_contract(single("pass"))


def test_two_names_that_become_one_python_name_are_refused() -> None:
    flow = Workflow("w")
    a, b = flow.state("in-progress"), flow.state("in_progress")
    flow.transition(START, a)
    flow.transition(a, b, on=flow.event("go"))
    flow.transition(b, END, on="go")

    with pytest.raises(
        CodegenError, match="'in-progress' and 'in_progress' are both 'in_progress'"
    ):
        generate_contract(compile_workflow(flow))


class Context(BaseModel):
    repo: str


def test_a_model_may_not_take_a_name_the_contract_uses() -> None:
    flow = Workflow("w", context=Context)
    a = flow.state("a")
    flow.transition(START, a)
    flow.transition(a, END, on=flow.event("done"))

    with pytest.raises(CodegenError, match="a model is named 'Context'"):
        generate_contract(compile_workflow(flow))


# ── stubs ─────────────────────────────────────────────────────────────────


def test_a_stub_is_a_typed_routine_for_its_state(
    tmp_path: Path, sdlc_document: dict[str, Any]
) -> None:
    load(tmp_path, "contract", generate_contract(sdlc_document))
    source = generate_stub(sdlc_document, "testing")
    stub = load(tmp_path, "stub", source)

    assert source == (
        '"""The `testing` state of the `sdlc` workflow."""\n'
        "\n"
        "from stateous import on_state\n"
        "\n"
        "from contract import TicketContext, events, states\n"
        "\n"
        "\n"
        "@on_state(states.testing)\n"
        "def testing(ctx: TicketContext) -> events.passed | events.failed:\n"
        "    raise NotImplementedError(\"do the work of 'testing' here\")\n"
    )
    state = implementation_of(stub.testing)
    assert state is not None and state.name == "testing"
    with pytest.raises(NotImplementedError, match="testing"):
        stub.testing(None)


def test_a_stub_is_formatted_as_ruff_formats_it(
    tmp_path: Path, sdlc_document: dict[str, Any]
) -> None:
    path = tmp_path / "stub.py"
    path.write_text(generate_stub(sdlc_document, "coding"), encoding="utf-8")

    result = ruff_formatted(path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_stub_without_a_context_model_or_events(tmp_path: Path) -> None:
    flow = Workflow("plain")
    waiting = flow.state("in-waiting", timeout="1h")
    flow.transition(START, waiting)
    flow.transition(waiting, END, on="timeout")
    document = compile_workflow(flow)
    load(tmp_path, "contract", generate_contract(document))

    source = generate_stub(document, "in-waiting")
    stub = load(tmp_path, "stub", source)

    assert "from stateous import Context, on_state\n" in source
    assert "from contract import states\n" in source
    assert "def in_waiting(ctx: Context) -> None:" in source
    assert implementation_of(stub.in_waiting) is not None


def test_a_stub_for_a_state_that_does_not_exist_is_refused(
    sdlc_document: dict[str, Any],
) -> None:
    with pytest.raises(CodegenError, match="the workflow 'sdlc' has no state 'review'"):
        generate_stub(sdlc_document, "review")
