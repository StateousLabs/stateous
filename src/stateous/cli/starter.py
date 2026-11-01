"""The starter workflow that `stateous init workflow NAME` writes (see the docs, Getting
started): `flow.py` and `schemas.py`, in the folder `NAME/`.

The text is built in Python, as `stateous.compiler.codegen` builds `contract.py`. The
starter compiles as it is: one state, `work`, and one event, `done`, whose payload the
context keeps. Its comments give the two rules: where data goes, and who decides."""

from __future__ import annotations

FLOW = "flow.py"
SCHEMAS = "schemas.py"


def flow_text(name: str) -> str:
    """The text of `NAME/flow.py`, for the workflow `name`. The name is valid (a-z, 0-9,
    `_` and `-`), so it goes into the text as it is."""
    return f"""\
# {name}/flow.py: the workflow. `stateous compile {name}/flow.py` checks it and writes
# the graph file, {name}/build/{name}.json.
from stateous import END, START, Workflow

from schemas import Job, Result

# The workflow. Each token has a context: a Job (see schemas.py). The context is
# the data of a token. A routine reads and writes only the context.
flow = Workflow("{name}", context=Job)

# The states. A token is in one state at a time. A routine does the work of a state.
work = flow.state("work")

# The events. An event moves a token. Its payload says why the token moved.
# `into="result"` keeps the payload in the context, as ctx["result"].
done = flow.event("done", payload=Result, into="result")

# The transitions: where each event takes a token. START and END begin and end each token.
# A routine decides which event to return. A guard (`when=`) only chooses a transition.
# See https://stateouslabs.github.io/stateous/concepts/data/
flow.transition(START, work)
flow.transition(work, END, on=done)
"""


def schemas_text(name: str) -> str:
    """The text of `NAME/schemas.py`: the context model and one payload model."""
    return f'''\
# {name}/schemas.py: the models. The context is the data of a token.
# A payload is the data of an event.
from pydantic import BaseModel


class Result(BaseModel):
    """The payload of the event `done`."""

    summary: str


class Job(BaseModel):
    """The context of each token of `{name}`."""

    title: str
    result: Result | None = None  # `done` keeps its payload here
'''
