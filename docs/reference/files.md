# The files

This page tells what each file of the two folders holds, and which command writes it.

```text
orders/                      the workflow's folder: the control plane's side
  flow.py                    you write it: states, events, transitions, guards
  schemas.py                 you write it: the context and payload models
  pyproject.toml             written by `stateous init workflow`
  build/orders.json          written by `stateous compile`: the graph file

orders-supervisor/           the supervisor's folder: on any computer
  contract.py                written by `stateous init supervisor`, each time
  draft.py, review.py        a stub for each state, then your routines
  stateous.yaml              written one time; then you edit it
  pyproject.toml             written one time; then you edit it
  .stateous/outbox.db        written by `stateous supervise`
```

## The workflow's folder

### `flow.py` and `schemas.py`

`flow.py` holds the `Workflow`: its states, events and transitions. `schemas.py` holds the Pydantic models of the context and the payloads. `stateous init workflow` writes a starter of each. Then they are yours. [Write a workflow](../how-to/write-a-workflow.md) tells how to change them.

### The graph file: `build/<workflow>.json`

`stateous compile` writes it from `flow.py`. `stateous serve` reads it. It is JSON, and you do not edit it.

| Key | What it holds |
|---|---|
| `stateous` | The format of the graph file. It is `1`. |
| `workflow` | The workflow's name. |
| `context` | The name of the context model, if the workflow has one. |
| `states` | Each state, with its `emits`, and its `timeout` and `retries` if they are set. A parallel state has `parallel`, `join` and `inputs`. |
| `events` | Each event, with the name of its `payload` model if it has one. |
| `transitions` | Each transition: `from`, `to`, `on`, and `when` or `otherwise` for a guard. START is `__start__` and END is `__end__`. |
| `schemas` | The JSON Schema of each model. |
| `workflows` | Only with a parallel state: each child workflow, with its own `states`, `events` and `transitions`. |

A part of the graph file from the `sdlc` example:

```json
{
  "stateous": 1,
  "workflow": "sdlc",
  "context": "Ticket",
  "states": {
    "coding": {"timeout": "6h", "retries": 2, "emits": ["done", "stuck"]},
    "testing": {"timeout": "1h", "emits": ["passed", "failed"]}
  },
  "transitions": [
    {"from": "testing", "to": "coding", "on": "failed", "when": "count('failed') < 3"},
    {"from": "testing", "to": "__end__", "on": "failed", "otherwise": true}
  ]
}
```

The checksum of the graph is `sha256:` and the hash of its JSON. `GET /health` gives it. A change to only `timeout` or `retries` does not change the structure of the graph.

### `pyproject.toml`

The folder is a Python project of its own. `stateous init workflow` writes this file only when the folder does not have it. It depends on Stateous, at the version that wrote it or a newer one. Add what `flow.py` and `schemas.py` import to `dependencies`.

## The supervisor's folder

### `contract.py`

`stateous init supervisor` writes it each time, in place of the old file. Do not edit it. It is all that the supervisor knows of the workflow:

- the workflow's name and the graph's checksum, as `__workflow__` and `__checksum__`;
- the context and payload models, as Pydantic classes;
- the class `events`, with one class for each event and its payload type;
- the class `states`, with one `StateRef` for each state and the events that it can emit.

A state of a child workflow is in a class of its own, for example `states.security_review.triage`.

```python
# GENERATED from the `sdlc` workflow — do not edit.
from stateous import Context, Occurrence, StateRef

__workflow__ = "sdlc"


class events:
    class done(Occurrence[None]):
        name = "done"


class states:
    coding = StateRef("coding", emits=(events.done,))
```

`stateous supervise` does not start when the checksum in `contract.py` differs from the control plane's. Run `stateous init supervisor` again after each change to the workflow.

### The stubs

`stateous init supervisor` writes one stub for each state that no file of the folder implements. A parallel state has no stub, because its work is its child workflows. The init never writes over a file that exists.

- The stub of the state `coding` is `coding.py`.
- A state of a child workflow gets `<workflow>_<state>.py`.
- A stub that would hide a Python module gets `<state>_routine.py`. For example, the state `queue` gets `queue_routine.py`.
- When two states get the same file name, the second stub gets `<name>_2.py`. The line of `init` tells why.

A stub holds one routine that raises `NotImplementedError`. Its return type names the events of the state:

```python
"""The `coding` state of the `sdlc` workflow."""

from stateous import on_state

from contract import TicketContext, events, states


@on_state(states.coding)
def coding(ctx: TicketContext) -> events.done | events.stuck:
    raise NotImplementedError("do the work of 'coding' here")
```

### `stateous.yaml`

The supervisor's settings. `stateous init supervisor` writes it only when the folder does not have it. From a graph file or a `flow.py`, it writes this text:

```yaml
# A Stateous supervisor. Run it with: stateous supervise <this folder>
url: "http://127.0.0.1:8080"    # the control plane
tasks: 4    # how many routines run at once
# name: build-box-1    # optional; default: the host name
```

To give the supervisor a name, remove the `#` before `name:`.

| Key | Meaning |
|---|---|
| `url` | The control plane: an `http://` or `https://` address. Required. With a URL as the source, `init` writes that URL. With a graph file or a `flow.py`, it writes `http://127.0.0.1:8080`. |
| `tasks` | How many routines run at once: 1 to 1000. The default is 4. |
| `name` | The supervisor's name in the token's history: 1 to 128 characters. The default is the host name. |

A wrong key value stops `stateous supervise` with one line that names the key.

### `pyproject.toml`

The supervisor's folder is also a Python project of its own. `stateous init supervisor` writes this file only when the folder does not have it. Add the packages that your routines import to `dependencies`. Then `uv sync` in the folder makes its environment, `.venv`.

With `--stateous-path PATH`, the file also has a `[tool.uv.sources]` table. The table points `stateous` at a local checkout, to work on Stateous itself:

```toml
[project]
name = "sdlc-supervisor"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["stateous>=0.2.2"]

# A local checkout of Stateous: delete this table to use Stateous from PyPI.
[tool.uv.sources]
stateous = { path = "C:/work/stateous", editable = true }
```

Delete the table to use Stateous from PyPI again.

### `.stateous/outbox.db`

The supervisor's outbox: one SQLite file. `stateous supervise` writes each report, note and memory write to it first, then sends it. A message leaves the outbox when the control plane accepts it or refuses it.

At start, the supervisor sends the messages that are left, in order. Then it claims new tasks. The outbox has a lock: a second supervisor on the same folder does not start. Do not delete the file while messages are in it.

`.stateous/` holds the local state of the supervisor. Do not commit it: add `.stateous/` to the folder's `.gitignore`.

## The control plane's database

Without `--db`, `stateous serve` keeps its data in `stateous.db`, a SQLite file in the current folder. [The data](../inside/data.md) tells what each table holds.

## See also

- [The commands](cli.md): the commands that write these files.
- [The two sides](../concepts/two-sides.md): why the supervisor has a contract and not the graph.
- [Run supervisors](../how-to/run-supervisors.md): set `stateous.yaml`, and start the supervisor.
