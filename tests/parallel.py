"""A workflow with a parallel state (see the guide, "Nested and parallel work"),
shared by the tests of phase 6.

`sdlc` codes, then runs two reviews at the same time: a child token of the
`security` workflow and one of the `design` workflow. A review ends in `approved` or
`rejected`, and records its verdict before it ends. When each review has ended,
`sdlc` merges if both were approved; else it codes again."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from stateous import END, START, Workflow
from stateous.compiler.compile import compile_workflow

TOP = "T-1"
SECURITY = "T-1.reviews.1.security"
DESIGN = "T-1.reviews.1.design"
ALL_APPROVED = "branches.all(status == 'completed' and final == 'approved')"


class Ticket(BaseModel):
    repo: str
    branch: str = "main"


class Review(BaseModel):
    repo: str
    branch: str


class Findings(BaseModel):
    count: int


def review(name: str) -> Workflow:
    """A review: `triage`, then `approved` (on `lgtm`) or `rejected` (on `deny`, with
    its findings). `recorded` ends it."""
    child = Workflow(name, context=Review)
    triage = child.state("triage")
    approved = child.state("approved")
    rejected = child.state("rejected")
    recorded = child.event("recorded")
    child.transition(START, triage)
    child.transition(triage, approved, on=child.event("lgtm"))
    child.transition(triage, rejected, on=child.event("deny", payload=Findings))
    child.transition(approved, END, on=recorded)
    child.transition(rejected, END, on=recorded)
    return child


def parallel_workflow(join: str = "all") -> Workflow:
    """`sdlc` with the parallel state `reviews`, which has a timeout of two days."""
    flow = Workflow("sdlc", context=Ticket)
    coding = flow.state("coding")
    reviews = flow.state(
        "reviews",
        parallel=[review("security"), review("design")],
        join=join,
        inputs=["repo", "branch"],
        timeout="2d",
    )
    merging = flow.state("merging")
    blocked = flow.state("blocked")
    done = flow.event("done")
    flow.transition(START, coding)
    flow.transition(coding, reviews, on=done)
    flow.transition(reviews, merging, on="joined", when=ALL_APPROVED)
    flow.transition(reviews, coding, on="joined", otherwise=True)
    flow.transition(reviews, blocked, on="timeout")
    flow.transition(merging, END, on=flow.event("merged"))
    flow.transition(blocked, coding, on=done)
    return flow


def parallel_document(join: str = "all") -> dict[str, Any]:
    """`parallel_workflow(join)`, compiled: the graph document."""
    return compile_workflow(parallel_workflow(join))
