"""Shared test data: the example workflow of docs/design.md, section 4.1."""

from typing import Any

import pytest
from pydantic import BaseModel

from stateous import END, START, Workflow
from stateous.compiler.compile import compile_workflow


class Ticket(BaseModel):
    repo: str


class Tests(BaseModel):
    passed: int
    failed: int


def sdlc_workflow() -> Workflow:
    """The workflow of design section 4.1, as it is written there."""
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
    return flow


@pytest.fixture
def sdlc() -> Workflow:
    return sdlc_workflow()


@pytest.fixture
def sdlc_document() -> dict[str, Any]:
    return compile_workflow(sdlc_workflow())
