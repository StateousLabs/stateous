"""The starter workflow that `stateous init workflow NAME` writes (see the docs, Getting
started): `flow.py` and `schemas.py`, in the folder `NAME/`.

The text is built in Python, as `stateous.compiler.codegen` builds `contract.py`. The
starter compiles as it is: two states, two events (one with a payload), and a guard."""

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

from schemas import Item, Verdict

# The workflow and its name. Each token has a context: an Item (see schemas.py).
flow = Workflow("{name}", context=Item)

# The states. A token is in one state at a time. A routine does the work of a state.
# `timeout=` and `retries=` are optional. See
# https://stateouslabs.github.io/stateous/how-to/write-a-workflow/
draft = flow.state("draft")
review = flow.state("review")

# The events: the outcomes that move a token. A payload is optional.
submitted = flow.event("submitted")  # no payload
reviewed = flow.event("reviewed", payload=Verdict)  # the payload is a Verdict

# The transitions: where each event takes a token. START and END begin and end each token.
flow.transition(START, draft)  # a new token goes to `draft` at once
flow.transition(draft, review, on=submitted)
# A guard (`when=`) is optional. Here, the payload of `reviewed` chooses the target.
flow.transition(review, END, on=reviewed, when="payload.approved == true")
# `otherwise=True` marks the transition to use when no guard matches.
flow.transition(review, draft, on=reviewed, otherwise=True)
"""


def schemas_text(name: str) -> str:
    """The text of `NAME/schemas.py`: the context model and one payload model."""
    return f'''\
# {name}/schemas.py: the models. The context is the data of a token.
# A payload is the data of an event.
from pydantic import BaseModel


class Item(BaseModel):
    """The context of each token of `{name}`."""

    title: str


class Verdict(BaseModel):
    """The payload of the event `reviewed`."""

    approved: bool
    note: str = ""
'''
