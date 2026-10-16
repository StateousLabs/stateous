# Write a workflow

This guide tells you how to change the `orders` starter into a workflow for your own process, and how to read a compile error.

The example is `sdlc`: a ticket goes to `coding`, then to `testing`, and back to `coding` when the tests fail. The words are defined in [The model](../concepts/model.md).

## Before you start

You have a workflow's folder. Make one with `init`, or use the `orders` folder of [Getting started](../getting-started.md):

```bash
stateous init workflow sdlc
```

The folder has `flow.py`, `schemas.py` and `pyproject.toml`. The workflow is on the control plane's side. A supervisor never reads these files.

## 1. Write the models

Write the models in `schemas.py`. A model is a pydantic model.

- The **context** model gives the fields of each token's business data.
- A **payload** model gives the fields of the data of one event.

```python
# sdlc/schemas.py
from pydantic import BaseModel


class Ticket(BaseModel):
    repo: str  # the path of the checkout, on the supervisor's computer


class Tests(BaseModel):
    passed: int
    failed: int
```

Values must be JSON. A routine reads the context as `ctx["repo"]`.

## 2. Declare the workflow, its states and its events

In `flow.py`, make the workflow with `Workflow(name, context=Model)`. Then add the states and the events.

| Call | What it does |
|---|---|
| `Workflow(name, context=Model)` | A workflow. `context` is optional. |
| `flow.state(name, timeout=None, retries=0)` | A state. After `timeout`, the token gets the event `timeout`. A failed task is tried again `retries` times. Then the token gets the event `error`. |
| `flow.state(name, parallel=[wf, ...], join="all", inputs=[keys], timeout=None)` | A parallel state. See [Parallel and nested work](../concepts/parallel.md). |
| `flow.event(name, payload=Model)` | An event. `payload` is optional. |
| `flow.transition(source, target, on=event, when=None, otherwise=False)` | Where an event takes a token. |

`retries=2` gives three attempts in all. `timeout=` covers all attempts of a task. See [Timers and timeouts](../concepts/timers.md).

## 3. Add the transitions

Each transition goes from a source state, on an event, to a target state. Start each token with a transition from `START`, without `on=`. End a token with a transition to `END`.

When one event can go to two targets, give each transition a guard with `when=`. Mark one transition `otherwise=True`: it applies when no guard matches. See [Guards](../concepts/guards.md).

This is the full `flow.py` of `sdlc`:

```python
# sdlc/flow.py
from stateous import END, START, Workflow

from schemas import Tests, Ticket

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
```

## 4. Compile

```bash
stateous compile sdlc/flow.py
```

`compile` loads `flow.py`, checks the workflow, and writes the graph file `sdlc/build/sdlc.json`. It prints the path of the graph file. To write the graph file to a different place, use `--out FILE`.

Then serve the graph file, and make the supervisor's folder:

```bash
stateous serve sdlc/build/sdlc.json
stateous init supervisor sdlc/flow.py sdlc-supervisor
```

To change a workflow that has live tokens, read [Change a workflow](change-a-workflow.md) first.

## The rules

- A name of a workflow, a state or an event has lower-case letters, digits, `_` and `-`. It starts with a letter and has 64 characters or fewer.
- A duration is a whole number and one unit: `30s`, `10m`, `6h` or `2d`. The longest is `3650d`.
- A transition from `START` has no `on=`. Nothing goes into `START`, and nothing leaves `END`.
- `timeout`, `error` and `joined` are Stateous's own events. Use them by name, for example `on="timeout"`. You cannot declare them or fire them.
- If no transition takes `timeout` or `error`, the token fails.
- `flow.py` has one workflow that no parallel state runs. Declare the child workflows of parallel states in the same file.

## Read a compile error

A compile error starts with `error:`, and the exit code is 1. `compile` writes no graph file. When the workflow has more than one problem, the error lists each of them.

This workflow has a state that no transition reaches, and two guards that can both be true:

```text
error: workflow 'orders' is not valid:
  - state 'shipping' cannot be reached from START
  - 'reviewed' from 'review': the guards 'payload.approved == true' and "payload.note != ''" can both be true, e.g. when payload.approved = True, payload.note = '\x00'; make them exclusive, or make one the fallback (otherwise=True)
```

Each line names the state, the event or the guard, and tells how to correct it. For two guards, compile also gives values that make both guards true.

Some mistakes stop `flow.py` before the checks. Then the error names the line of `flow.py` or the import that failed:

```text
error: state 'review': '2 hours' is not a duration; write e.g. '30s', '10m', '2h', '1d'
```

These are frequent errors, and what to do:

| The error says | What to do |
|---|---|
| `state 'X' cannot be reached from START` | Add a transition into `X`, or remove the state. |
| `has no transition from START` | Add `flow.transition(START, <state>)`. |
| `has no transition into END` | Add a transition to `END`, so that a token can complete. |
| `the guards ... can both be true` | Make the guards exclusive, or make one transition `otherwise=True`. |
| `has guarded transitions and an unguarded one` | Mark the transition without a guard `otherwise=True`. |
| `is not a duration` | Write a duration such as `30s`, `10m`, `6h` or `2d`. |
| `does not import` | Correct the import in `flow.py` or `schemas.py`. |
| `defines no Workflow at module level` | Make the workflow at the top level of `flow.py`. |

## See also

- [The model](../concepts/model.md): the words [workflow](../concepts/model.md#workflow), [state](../concepts/model.md#state), [event](../concepts/model.md#event) and [transition](../concepts/model.md#transition).
- [Guards](../concepts/guards.md): the guard language, and the overlap check.
- [Timers and timeouts](../concepts/timers.md): `timeout=` and the event `timeout`.
- [Write a routine](write-a-routine.md): the work of each state.
- [Change a workflow](change-a-workflow.md): a new graph while tokens are live.
