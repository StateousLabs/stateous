# The two sides

## In one sentence

The control plane keeps the graph and every token, and a supervisor does the work of states without seeing the graph.

## Why it exists

The work of a state often runs on a special computer: a build server, a GPU computer, or a laptop with an agent's login. The process itself must be kept safe in one place.

So Stateous has two sides. The control plane decides, and the supervisors do the work. You can change where the work runs, and you can add supervisors, without a change to the graph.

The supervisor never sees the graph, for two reasons:

- A routine cannot depend on the next state. It can only return an outcome, so you can change the graph without a change to the routines.
- A supervisor has only the information that its work needs. The rest of the process stays on the control plane.

## How it works

Each side is a folder of its own.

| Folder | Side | What it holds |
|---|---|---|
| `sdlc/` | the control plane | `flow.py`, `schemas.py`, and the graph file `build/sdlc.json` |
| `sdlc-supervisor/` | a supervisor | `contract.py`, one routine file for each state, `stateous.yaml`, `pyproject.toml` |

The graph file stays on the control plane's side. Two things cross to the supervisor's side:

- **The contract.** `stateous init supervisor` writes `contract.py`. It gets the graph from the control plane with `GET /workflow`, or it reads a local graph file or `flow.py`. The contract keeps only the states, the events that each state can emit, and the models.
- **HTTP.** The supervisor claims tasks, sends heartbeats, and reports outcomes. It also sends notes and memory writes. It does this over HTTP, to the control plane's URL in `stateous.yaml`.

```mermaid
flowchart LR
    subgraph cp["control plane side"]
        flow["flow.py + schemas.py"] -->|stateous compile| graph["graph file"]
        graph --> serve["control plane<br/>stateous serve"]
        serve --> db[("database")]
    end
    subgraph sv["supervisor side"]
        contract["contract.py"] --> routines["routines"]
        routines --> supervisor["supervisor<br/>stateous supervise"]
    end
    serve -->|"GET /workflow, once:<br/>stateous init supervisor"| contract
    supervisor <-->|"HTTP: claim, heartbeat,<br/>report, note, memory"| serve
```

At start, the supervisor compares the checksum in `contract.py` with the checksum that the control plane serves. When they differ, the supervisor does not start. Run `stateous init supervisor` again after each change to the workflow.

## Example

Make the folder of a supervisor from a control plane that runs:

```bash
stateous init supervisor http://127.0.0.1:8080 sdlc-supervisor
```

This is the `contract.py` that it writes for `sdlc`. It has no transitions and no guards:

```python
# GENERATED from the `sdlc` workflow — do not edit.
# sha256:ce35bf077678e6aa975cf404a1417ecdb9f5283b037a34d00d5418cbc9123881
from __future__ import annotations

from pydantic import BaseModel

from stateous import Context, Occurrence, StateRef

__workflow__ = "sdlc"
__checksum__ = "sha256:ce35bf077678e6aa975cf404a1417ecdb9f5283b037a34d00d5418cbc9123881"


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
```

The routine of `testing` knows that it can emit `passed` or `failed`. It does not know that a `failed` can send the token back to `coding`.

## Guarantees

- The supervisor's folder never holds `flow.py` or the graph file. Its routines import only `contract.py`.
- A supervisor does not start when its `contract.py` is for a different graph.
- The control plane checks each outcome again. An event that the state cannot emit, or a payload that does not fit its model, is refused.
- Not guaranteed: who can call the control plane. Stateous has no authentication. Put it behind a VPN or a proxy.

## See also

- [The model](model.md): the words [control plane](model.md#control-plane), [supervisor](model.md#supervisor) and [contract](model.md#contract).
- [Tasks and routines](tasks-and-routines.md): what crosses the network for each task.
- [Getting started](../getting-started.md): both sides on one computer.
- [Reliability](reliability.md): what keeps the two sides safe when a network or a computer fails.
- [Run supervisors](../how-to/run-supervisors.md): supervisors on other computers.
- [Change a workflow](../how-to/change-a-workflow.md): run `stateous init supervisor` again after a change.
- [The files](../reference/files.md): the graph file, `contract.py` and the other files of each folder.
