# The Stateous guide

This page tells you how to use Stateous. It uses one example from start to end: [`examples/sdlc/`](../examples/sdlc/) and [`examples/sdlc-supervisor/`](../examples/sdlc-supervisor/).

## What Stateous does

You write a process as a graph of states. A control plane keeps every token of the process durable. It also chooses each next state. A supervisor, on any computer, does the work and reports what happened.

The supervisor never sees the graph. It knows only which states it serves, and which outcomes each state allows.

## The words

| Word | Meaning |
|---|---|
| **Workflow** | The graph: states, events and transitions. You write it in Python and compile it to a graph file. |
| **State** | A place where a token can be. When a token enters a state, the control plane makes one task. |
| **Event** | An outcome, with an optional payload. Only an event moves a token. |
| **Token** | One run of the workflow, with its own context. |
| **Task** | "Do state S for token T". The control plane queues it. A supervisor takes it. |
| **Routine** | Your function that does the work of one state. It is in the supervisor's folder. |
| **Contract** | A generated file. For each state, it gives the events that the state can emit, and their payload types. It is all that the supervisor knows of the workflow. |
| **Supervisor** | The process that takes tasks, runs routines, watches the work that they start, and reports. |

## Two sides, two folders

```
sdlc/                    the control plane's side: the workflow
  flow.py                states, events, transitions, guards
  schemas.py             the context and payload models
  build/sdlc.json        written by `stateous compile`: the graph file

sdlc-supervisor/         the supervisor's side, on any computer: no graph
  contract.py            written by `stateous init`: states, their events, the models
  coding.py              @on_state(states.coding) ...
  testing.py             @on_state(states.testing) ...
  stateous.yaml          the control plane's URL, how many tasks at once
  pyproject.toml         written by `stateous init` if absent: the folder is a Python project
  .stateous/outbox.db    made by `stateous supervise`: messages not yet delivered
```

The supervisor's folder never holds `flow.py` or the graph file. Its routines import only `contract.py`.

`.stateous/` holds the local state of the supervisor. Do not commit it: add `.stateous/` to the folder's `.gitignore`.

## Install

Stateous needs Python 3.12 or newer. It is not on PyPI yet. Add it to your project from a copy of its repository:

```bash
uv add --editable path/to/stateous
```

Run the `stateous` command in that environment, for example with `uv run stateous`. The control plane keeps its data in SQLite or in Postgres.

## Write the workflow

The workflow is on the control plane's side. First, write the models. The context is the business data of a token. A payload is the data of an event.

```python
# sdlc/schemas.py
from pydantic import BaseModel


class Ticket(BaseModel):
    repo: str  # the path of the checkout, on the supervisor's computer


class Tests(BaseModel):
    passed: int
    failed: int
```

Then write the graph:

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

| Call | What it does |
|---|---|
| `Workflow(name, context=Model)` | A workflow. `context` is optional. |
| `flow.state(name, timeout=None, retries=0)` | A state. After `timeout`, the token gets the event `timeout`. A failed task is tried again `retries` times. Then the token gets the event `error`. |
| `flow.state(name, parallel=[wf, ...], join="all", inputs=[keys], timeout=None)` | A parallel state: its work is one child token for each workflow. See "Nested and parallel work". |
| `flow.event(name, payload=Model)` | An event. `payload` is optional. |
| `flow.transition(source, target, on=event, when=None, otherwise=False)` | Where an event goes. `START` and `END` begin and end every token. |

Rules:

- A transition from `START` has no `on=`.
- A name of a state or an event has lower-case letters, digits, `_` and `-`. It starts with a letter and has 64 characters or fewer.
- A duration is a whole number and one unit: `30s`, `10m`, `6h` or `2d`. The longest is `3650d`.
- Stateous has three events of its own: `timeout`, `error` and `joined`. Use them by name, for example `on="timeout"`. You cannot declare them, and you cannot fire them.
- If no transition takes `timeout` or `error`, the token fails.

### Guards

`when=` is a guard: a small expression. It can use these items:

- `ctx['key']`: a field of the token's context;
- `payload.field`: a field of the event's payload;
- `count('event')`: how many times the token accepted this event, this one included;
- comparisons (`<`, `<=`, `>`, `>=`, `==`, `!=`, `in`, `not in`), `and`, `or` and `not`;
- numbers (also negative numbers), strings, `true`, `false`, `null` and lists of these.

On the right of `in` and `not in`, use a list, or a value from `ctx`, `payload` or `count`. A string is not permitted there.

`stateous compile` makes sure that the guards on one state and event never overlap. If two transitions can both match, compile stops with an error. Stateous never chooses between them at run time. `otherwise=True` marks the transition to use when no guard matches.

If no guard matches and there is no `otherwise=True`, the control plane refuses the event. The token stays where it is.

In the example, a failed test run sends the token back to `coding` two times. The third `failed` ends the token.

## Compile and serve

```bash
stateous compile sdlc/flow.py          # writes sdlc/build/sdlc.json
stateous serve sdlc/build/sdlc.json    # the control plane, on 127.0.0.1:8080
```

`stateous compile` loads `flow.py`, checks the workflow, and writes the graph file. It prints the path of the graph file. An error names the mistake. Use `--out FILE` to write the graph file to a different place.

`stateous serve` opens the database. Then it starts the HTTP API and the clock. It prints `serving on http://127.0.0.1:8080 (Ctrl-C stops)`. It has these options:

- `--db URL`: the database. The default is `sqlite:///stateous.db`, in the current folder. For Postgres, use `postgresql://…`.
- `--listen HOST:PORT`: the address. The default is `127.0.0.1:8080`. A wider address gives a warning, because Stateous has no authentication.
- `--supervise FOLDER`: also run a supervisor for this folder, in the same process. Use it for development.

`stateous serve` does not start in these cases. It prints one sentence that tells why, and the exit code is 1.

- Another control plane serves the same database.
- The graph has a different structure, and the database has active or paused tokens of the old structure.
- The graph file, the database URL or the address is not correct, or the port is in use.

A change to only `timeout` or `retries` is not a change of structure. `serve` accepts it.

If another control plane takes the database later, `serve` stops with the exit code 1.

A supervisor on a different computer must reach the control plane. Then use `--listen 0.0.0.0:8080`. Put the control plane behind a VPN or a proxy.

## Set up a supervisor

```bash
stateous init http://cp.internal:8080 sdlc-supervisor
```

`stateous init` gets the graph from the control plane. Then it writes these files:

- `contract.py`, always (it replaces the old file);
- a stub `<state>.py` for each state that no file in the folder implements;
- `stateous.yaml`, only if the folder does not have it;
- `pyproject.toml`, only if the folder does not have it.

`init` never writes over a routine file, over `stateous.yaml` or over `pyproject.toml`. A stub that would hide a Python module gets the name `<state>_routine.py`. For example, the state `queue` gets `queue_routine.py`. `init` prints one line for each file:

```text
wrote    sdlc-supervisor/contract.py
wrote    sdlc-supervisor/coding.py
kept     the routine of 'testing'
wrote    sdlc-supervisor/stateous.yaml
wrote    sdlc-supervisor/pyproject.toml

Write the routines, then run: stateous supervise sdlc-supervisor
```

On Windows, the paths in these lines have `\` in place of `/`.

The folder is a Python project of its own. Its `pyproject.toml` depends on Stateous. Add the packages that your routines import to `dependencies`. Then `uv sync` in the folder makes its environment, `.venv`.

Stateous is not published yet. Until it is, give `init` the folder of a local Stateous checkout with `--stateous-path`:

```bash
stateous init http://cp.internal:8080 sdlc-supervisor --stateous-path ../stateous
```

Then `pyproject.toml` has a `[tool.uv.sources]` table that points `stateous` at that checkout. Delete the table when Stateous is published. `init` does not write the folder if the path is not a Stateous checkout.

To edit the routines in VS Code, open the supervisor's folder and run `uv sync` in it. Then select `.venv` as the Python interpreter. VS Code then finds `stateous` and `contract.py`.

Run `stateous init` again after each change to the workflow. `stateous supervise` does not start when `contract.py` does not agree with the control plane.

The settings of the supervisor are in `stateous.yaml`:

```yaml
url: http://127.0.0.1:8080     # the control plane
tasks: 4                       # routines at once
name: build-box-1              # optional; the default is the host name
```

`url` is an `http://` or `https://` address. `tasks` is 1 to 1000. The supervisor's `name` shows in the token's history.

## Write a routine

A routine does the work of one state. It is in the supervisor's folder, and it imports only `contract.py`.

```python
# sdlc-supervisor/testing.py
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree

from stateous import on_state

from contract import Tests, TicketContext, events, states  # generated


def pytest_in(checkout: Path, report: Path) -> None:
    """Run pytest in `checkout`, in a process group of its own. Then Ctrl-C to the
    supervisor does not stop the tests, and the supervisor waits for them."""
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    command.append(f"--junitxml={report}")
    if sys.platform == "win32":
        subprocess.run(
            command,
            cwd=checkout,
            capture_output=True,
            check=False,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        subprocess.run(
            command, cwd=checkout, capture_output=True, check=False, start_new_session=True
        )


def run_tests(repo: str) -> Tests:
    """Run the tests of the checkout `repo`. Count the tests that passed and failed."""
    checkout = Path(repo)
    if not checkout.is_dir():
        raise FileNotFoundError(f"no checkout at {checkout.resolve()}")
    with tempfile.TemporaryDirectory() as folder:
        report = Path(folder) / "report.xml"
        pytest_in(checkout, report)
        if not report.is_file():
            raise RuntimeError(f"pytest wrote no report for {checkout}")
        root = ElementTree.parse(report).getroot()
    suite = root if root.tag == "testsuite" else root[0]
    failed = int(suite.get("failures", "0")) + int(suite.get("errors", "0"))
    passed = int(suite.get("tests", "0")) - failed - int(suite.get("skipped", "0"))
    return Tests(passed=passed, failed=failed)


@on_state(states.testing)
def test(ctx: TicketContext) -> events.passed | events.failed:
    result = run_tests(ctx["repo"])
    return events.passed(result) if result.failed == 0 else events.failed(result)
```

`@on_state(states.X)` marks a routine. A routine is a `def` or an `async def`. Its argument is `ctx`:

| Field | Meaning |
|---|---|
| `ctx["key"]` | The token's context: its business data. A routine can write it: `ctx["key"] = value`. The change is saved with the outcome, or not at all. |
| `ctx.get("key", default)` | The same as `ctx["key"]`, with a default value. `ctx["key"]` raises `KeyError` for a key that is not set. |
| `ctx.memory` | The routine's own memory, for this token **and** this state. It is saved at once. It stays across crashes, retries and later visits to the state. It is deleted when the token ends. Guards cannot read it. |
| `ctx.note(text)` | One line of progress in the token's history: up to 500 characters. It is sent at once. Empty text does nothing. |
| `ctx.token` | The token's id. |
| `ctx.task` | The task's id. |
| `ctx.visit` | 1 the first time the token is in this state, 2 the next time, and so on. |
| `ctx.attempt` | 1, then 2 after a retry. |
| `ctx.entered_by` | The event that brought the token here: `.name` and `.payload`. In the token's first state, `.name` is `__started__`. |
| `ctx.schedule(event, after="10m")` | A timer. Later, the clock fires `event` on this token, if the token is still in this state. |

A value that you read from `ctx` is a copy. So `ctx["items"].append(2)` changes nothing. Write the new value with `ctx["items"] = ...`. Values must be JSON.

A routine returns one of these:

- **an event**, for example `events.passed(result)`: the token moves;
- **`None`**: the work continues outside, for example in CI or with a person. They report the outcome later, through the API.

If a routine raises an exception, the task fails. The control plane tries the task again while `retries` allows. Then the token gets `error`. A failed attempt saves no change to the context.

These also fail the task at once, with the reason in the history:

- an event that the state cannot emit;
- an event that no transition takes, because no guard matches;
- context changes that do not agree with the context model.

### Timers

`ctx.schedule(event, after=...)` sets a timer. Give an event without a payload, for example `events.done`. For an event with a payload, give the event with its payload, for example `events.failed(result)`.

The timer is set when the outcome is saved. When the token leaves the state, its timers stop. A retry stops the timers of the failed attempt, because the routine runs again. If no transition takes the timer's event, the history records the refusal, and the token stays.

The state's own `timeout` is a timer too. It covers all attempts of the task.

### Helpers for agents

For agents, the contract gives each state two helpers:

- `states.X.outcome_schema()`: the events that state X can emit, with their payloads, as one JSON schema;
- `states.X.outcome(data)`: the event that `data` names, checked against the schema. It raises `OutcomeError` if `data` does not fit.

### Run the supervisor

```bash
stateous supervise sdlc-supervisor
```

The supervisor runs at most `tasks` routines at once. An `async def` routine runs on the supervisor's event loop. A `def` routine runs in a thread. While a routine runs, the supervisor sends a heartbeat every 30 seconds.

When the token leaves the state, or is cancelled, the supervisor cancels an `async def` routine. It does this at the next heartbeat, so up to 30 seconds later. It cannot stop a `def` routine, so it ignores the result of that routine.

The supervisor logs to stderr:

```text
2026-10-06 23:43:39,848 stateous.supervisor: MorsyB serves testing; the control plane is http://127.0.0.1:8080
2026-10-06 23:43:48,279 stateous.supervisor: task 2: testing of TICKET-1, attempt 1
2026-10-06 23:43:51,708 stateous.supervisor: task 2 of TICKET-1: reported passed
```

To stop the supervisor, press Ctrl-C or send SIGTERM. It takes no new tasks, and it waits for its running routines. A second signal stops it at once, with the exit code 1.

If the supervisor cannot reach the control plane, its routines continue. It tries again after 1, 2, 4, 8 and 16 seconds, then every 30 seconds.

The supervisor keeps an outbox, `.stateous/outbox.db` in its folder. It writes each report, note and memory write to the outbox first. Then it sends it. The message leaves the outbox when the control plane accepts it or refuses it. When the control plane cannot be reached, the message stays. At start, the supervisor sends the messages that are left, in order. Then it claims new tasks. A message for a task that moved on is refused, and the supervisor drops it with one log line.

`stateous supervise` does not start in these cases:

- `contract.py` is for a different graph. Run `stateous init` again.
- The folder has no routine, or two routines for one state.
- A file of the folder cannot be imported.
- Another supervisor runs on the same folder. Two supervisors cannot share one outbox.

## Act on tokens

```bash
stateous token start TICKET-1 '{"repo": "acme/api"}'
stateous token fire TICKET-1 done --key gh-delivery-81231
stateous token show TICKET-1
stateous token show
stateous token show --graph
stateous token pause TICKET-1
stateous token resume TICKET-1
stateous token cancel TICKET-1
```

Each `token` command takes `--url`. The default is the environment variable `STATEOUS_URL`, else `http://127.0.0.1:8080`.

`start` with the id of a token that exists shows that token. It does not start a second token. `fire` takes an optional payload, as a JSON object after the event's name.

`show` with the id of a parent also lists its children, below the token's line.

`show` with an id shows the token and its history. The times are local:

```text
TICKET-1: testing, completed
23:43:39  #1  started
23:43:39  #2  __started__ in START
23:43:39  #3  entered coding
23:43:39  #4  task 1 queued for coding
23:43:39  #5  timer set: timeout at 2026-10-07 05:43:39
23:43:47  #6  done in coding
23:43:47  #7  task 1 cancelled
23:43:47  #8  entered testing
23:43:47  #9  task 2 queued for testing
23:43:47  #10  timer set: timeout at 2026-10-07 00:43:47
23:43:48  #11  MorsyB took task 2 (attempt 1)
23:43:51  #12  passed in testing: {"passed": 1, "failed": 0}
23:43:51  #13  task 2 done
23:43:51  #14  completed
```

A token leaves `START` at once, on the event `__started__`. Stateous makes this event itself. Line `#2` records it, and the first state's `ctx.entered_by.name` is `__started__`.

A token that ended keeps its last state in the first line. Here, `done` came from outside, so nobody took task 1. The history shows it as `cancelled`.

`show` with no id lists the tokens, at most 100. `show --graph` draws the workflow as a Mermaid diagram:

```text
stateDiagram-v2
    direction LR
    [*] --> coding
    coding --> testing: done
    coding --> [*]: stuck
    testing --> [*]: passed
    testing --> coding: failed [count('failed') < 3]
    testing --> [*]: failed [otherwise]
```

`--key` is an idempotency key. If a webhook comes two times with the same key, Stateous applies it one time. The second delivery gets the answer "duplicate":

```text
$ stateous token fire TICKET-1 done --key gh-delivery-1
TICKET-1: testing, active
$ stateous token fire TICKET-1 done --key gh-delivery-1
duplicate: TICKET-1: testing, completed
```

The duplicate shows the token as it is now. A key stays used for the life of the token, also across a pause.

An error prints one sentence to stderr, and the exit code is 1:

```text
$ stateous token fire TICKET-4 done
error: the control plane at http://127.0.0.1:8080 refused (409): token 'TICKET-4' is cancelled; it takes no events
```

The history also records a refused event, for example `done refused: ...`.

From Python, use the client:

```python
from stateous import Client

c = Client("http://cp.internal:8080")
c.start("TICKET-1", {"repo": "acme/api"})
c.fire("TICKET-1", "done", key="gh-delivery-81231")  # key: idempotency
c.get("TICKET-1")
c.history("TICKET-1")
c.tokens()
c.pause("TICKET-1")
c.resume("TICKET-1")
c.cancel("TICKET-1")
c.close()
```

`Client` is also a context manager: `with Client(url) as c: ...`.

A routine that returns `None` leaves its task waiting. The outside actor then calls `c.report(task, event, payload)` with the task's id. With `attempt=N`, the control plane refuses the report (409) when the task is not on attempt N. The client raises `Refused` (from `stateous.client`) when the control plane refuses a call. `Refused` has `.status`, `.message` and `.code`. `.code` is the code of the refusal, for example `attempt_over` (see [The HTTP API](#the-http-api)). It is `None` when the answer has no code, for example an answer from a proxy. Use `.code` in your program. `.message` is for people, and it can change. The client raises `Unreachable` when the control plane does not answer.

## Agents, with PHD

PHD is a library that runs coding agents. A routine calls PHD, and the supervisor watches the routine. PHD does not report to Stateous. It knows nothing of workflows.

```python
# sdlc-supervisor/coding.py: an agent, run by PHD (see the guide, "Agents, with PHD")
import phd

from stateous import on_state

from contract import TicketContext, events, states


@on_state(states.coding)
async def code(ctx: TicketContext) -> events.done | events.stuck:
    spec = phd.Spec(
        harness="claude",
        model="sonnet",
        stack=phd.Stack.load("stacks/dev"),  # the agent: a PHD stack folder
        cwd=ctx["repo"],
        credentials=phd.Credentials.from_host("claude"),  # the login of this computer
        output_schema=states.coding.outcome_schema(),
        idle_timeout_s=900,  # no event for 15 minutes: stop (PHD request 7)
    )
    inputs = phd.Inputs(msg=f"Implement {ctx['repo']}")
    # Leaving the block stops an unfinished run:
    # an error, or the token left the state (PHD request 1).
    async with phd.Dispatcher(spec).run(inputs, session_id=ctx.memory.get("dev")) as handle:
        async for ev in handle.events():
            if handle.session_id and "dev" not in ctx.memory:
                # known after run.started; kept across a crash (PHD request 2)
                ctx.memory["dev"] = handle.session_id
            ctx.note(ev.summary)  # progress in the token's history (PHD request 6)
        result = await handle.wait()
    if result.status != "succeeded":
        if result.error and result.error.kind == "session" and "dev" in ctx.memory:
            del ctx.memory["dev"]  # the session cannot be resumed: the retry starts fresh
        raise RuntimeError(result.error)  # a failed attempt
    return states.coding.outcome(result.structured_output)
```

PHD needs a stack and credentials for each run. `phd.Stack.load("stacks/dev")` loads the agent's stack folder: its identity, skills and tools. The path is relative to the folder where the supervisor runs. `phd.Credentials.from_host("claude")` uses the login of the supervisor's computer. The PHD documentation tells how to write a stack.

| Need | Who does it |
|---|---|
| Run the agent | PHD, called by an `async` routine. |
| Keep the task's lease alive while the agent runs | The supervisor. It sends heartbeats for each task whose routine still runs. |
| Stop the agent when the token leaves the state, or is cancelled | The supervisor cancels the routine. PHD's `async with … run(...)` block stops the run when it ends. |
| Choose the outcome | The agent. PHD's `output_schema` is `states.X.outcome_schema()`, so its answer names one allowed event and its payload. PHD checks the answer. A mismatch is a failed run, so a failed attempt. `states.X.outcome(...)` makes the event, and the control plane checks it again. |
| See that an agent is stuck | PHD. `Spec.idle_timeout_s` stops a run that sends no event for that long. |
| Show progress | The routine gives PHD's events to `ctx.note(...)`. |
| Retry and time out | Stateous, as for each routine. |

### Not released yet

`coding.py` uses PHD features that are not released yet. The PHD author accepted them on 2026-10-06:

- `Dispatcher.run()`, an `async with` block that stops the run when the block ends (batch 1);
- `handle.session_id`, known when the run starts (batch 1);
- `Spec.idle_timeout_s`, which stops a run with no events for that long (batch 1);
- `event.summary`, one line for each event (batch 3).

`coding.py` needs a PHD release with these features. Its tests use a stand-in for PHD.

`coding.py` gives each `event.summary` to `ctx.note(...)`. An empty summary does nothing, so this works for every event.

Until the release, use `start()`, and call `handle.cancel()` yourself:

```python
# Before PHD's batch 1: start(), and cancel the run yourself
import asyncio

import phd

from stateous import on_state

from contract import TicketContext, events, states


@on_state(states.coding)
async def code(ctx: TicketContext) -> events.done | events.stuck:
    spec = phd.Spec(
        harness="claude",
        model="sonnet",
        stack=phd.Stack.load("stacks/dev"),  # the agent: a PHD stack folder
        cwd=ctx["repo"],
        credentials=phd.Credentials.from_host("claude"),  # the login of this computer
        output_schema=states.coding.outcome_schema(),
    )
    inputs = phd.Inputs(msg=f"Implement {ctx['repo']}")
    handle = await phd.Dispatcher(spec).start(inputs, session_id=ctx.memory.get("dev"))
    try:
        result = await handle.wait()
    except asyncio.CancelledError:
        await handle.cancel()  # the token left the state: stop the agent
        raise
    if result.session_id:
        ctx.memory["dev"] = result.session_id
    if result.status != "succeeded":
        if result.error and result.error.kind == "session" and "dev" in ctx.memory:
            del ctx.memory["dev"]  # the session cannot be resumed: the retry starts fresh
        raise RuntimeError(result.error)
    return states.coding.outcome(result.structured_output)
```

With `start()`, the session id comes only at the end of the run. If the supervisor crashes, the retry starts a new session.

Install PHD in the supervisor's environment. Without PHD, the supervisor cannot import `coding.py`, and it does not start. Then remove `coding.py`, and report `coding` from outside, as in "Try it on one computer".

### The routine's memory

`ctx.memory` is a small key-value store. Stateous keeps one for each token **and** state. Use it for bookkeeping, for example an agent's session id. Do not use it for the workflow's data.

| Property | Behaviour |
|---|---|
| Scope | One token and one state. `TICKET-1` in `coding` and `TICKET-1` in `testing` have different memories. |
| Saving | At once (`PUT /tasks/{id}/memory`), not with the outcome. So it stays after a crash. |
| Values | JSON values. Keys have 1 to 128 characters. To delete a key, use `del ctx.memory["dev"]`. `None` as a value raises `ValueError`. |
| Retries | The retry sees it, so it can resume a crashed agent session. |
| Later visits | The token sees it again when it comes back to the state. An agent can continue its session ("now fix the failing tests"). |
| Several agents in one state | The routine chooses the keys: `memory["dev"]`, `memory["review"]`. |
| A new session for each visit | Put the visit in the key: `memory[f"dev:{ctx.visit}"]`. |
| End | Deleted when the token ends (completed, failed or cancelled). The history records the key of each change, never the value: `memory set: dev (in coding)`. |
| Guards | Cannot read it. |

### When the supervisor stops

The running agents of a supervisor are its child processes.

- **A graceful stop** (Ctrl-C, SIGTERM): the supervisor takes no new tasks, and waits for its running routines. A second signal stops it at once. A deploy loses nothing.
- **The control plane cannot be reached at the stop:** the supervisor does not wait for it. The reports, notes and memory writes stay in the outbox. The next `stateous supervise` sends them.
- **The lease limits the outbox:** a report counts only if it gets to the control plane before its task's lease ends. The lease ends 2 minutes after the last heartbeat. After that, the control plane counts a failed attempt and refuses the late report. Then the task runs again.
- **A crash** (or `kill -9`): today, PHD runs each agent in its own process group. So the agent does not stop with the supervisor. It can change the repository and spend money until it next writes output.
- PHD batch 1 ties the agent to its caller: a Windows Job Object, `PR_SET_PDEATHSIG` on Linux, a watchdog on macOS. After that, the agents stop with the supervisor.
- The leases of a dead supervisor end after 2 minutes. Then the tasks are tried again. With `ctx.memory`, the retry resumes the agent's session.
- **Steps with side effects:** the agent can repeat its last step or two. The agent's instructions must make that safe, for example "check whether the PR exists first". Stateous cannot do this for the agent.

## Reliability

Seven mechanisms keep tokens safe:

| Mechanism | What it protects |
|---|---|
| **The ledger** | Each change adds a row to one table. Tokens, tasks and timers change in the same transaction. A task is never lost between "the token moved" and "the task was queued". |
| **Leases** | A claimed task has a lease of 2 minutes. The supervisor's heartbeat renews it while the routine runs. When a supervisor dies, its leases end, and the tasks are tried again. |
| **The clock** | It fires timeouts and timers. A timeout is a timer that the state sets when a token enters it. |
| **Idempotency keys** | A webhook that comes two times is applied one time. The second delivery with the same key gets the answer "duplicate". |
| **The routine's memory** | An agent's session stays after a crash and a retry. |
| **The supervisor's outbox** | A report, a note or a memory write goes to the outbox (`.stateous/outbox.db`) first, then to the control plane. It leaves the outbox only when the control plane accepts it or refuses it. When the control plane cannot be reached, the outbox keeps a finished routine's report. The report counts only if it gets to the control plane before the task's lease ends (2 minutes after the last heartbeat). After that, the control plane has counted a failed attempt: it refuses the late report, and the task runs again. At start, `stateous supervise` sends what is left, in order, before it claims new tasks. Attempt numbers make an old message safe: the control plane refuses it, and the supervisor drops it. |
| **Pause and resume** | A paused token does not move. Its events wait, its tasks wait, and its timers stop. On resume, the waiting events apply in order. Events that no longer fit are refused and recorded. |

Also, one token moves one step at a time, because each change locks the token. One control plane serves one database. A second control plane does not start.

An ended lease counts as a failed attempt. The history tells why, for example `task 2 failed: build-box-1 stopped answering: no heartbeat for 2 minutes`.

A lost claim answer loses nothing. Each claim has a request id. If the answer does not come, the supervisor sends the claim again with the same id. The control plane then gives the same task again, with the same attempt, and renews its lease. It does not claim a second task.

### Pause and resume

```text
$ stateous token pause TICKET-3
TICKET-3: coding, paused
$ stateous token fire TICKET-3 done
TICKET-3: coding, paused
$ stateous token resume TICKET-3
TICKET-3: testing, active
```

The history shows `done held while paused`, then `resumed` and `done applied on resume`. A paused token works like this:

- It never moves or ends on its own. Its timers and its timeout stop. On resume, they get the paused time back.
- No supervisor takes its queued tasks.
- A routine that runs already continues, and its lease continues too. When the routine returns an event, the control plane holds all of the outcome until the resume: the event, the context changes and the timers.
- When the routine returns `None`, the task waits. The control plane saves its context changes and its timers at once. This outcome has no event, so the resume cannot drop it.
- On resume, a held outcome that still fits applies its context changes and timers, and then moves the token. A timer counts from the resume. The control plane drops a held outcome if the token left that state meanwhile, and applies none of its changes. The history records `dropped on resume`.
- A task that fails while the token is paused holds the event `error` until the resume.

Only an active token can be paused, and only a paused token can be resumed. `cancel` works on an active or a paused token. A cancelled token takes no more events.

## Nested and parallel work

The work of a state can be other workflows that run at the same time. Such a state is a parallel state.

```python
# The parent's workflow (part): two reviews at the same time
security = Workflow("security-review", context=Review)  # its own states, events, transitions
design = Workflow("design-review", context=Review)

reviews = flow.state("reviews", parallel=[security, design], join="all", inputs=["repo", "branch"])
flow.transition(coding, reviews, on=done)
flow.transition(
    reviews,
    merging,
    on="joined",
    when="branches.all(status == 'completed' and final == 'approved')",
)
flow.transition(reviews, coding, on="joined", otherwise=True)
```

| Argument | What it does |
|---|---|
| `parallel=[wf, ...]` | One child token for each workflow. The state has no routine and no task. |
| `join="all"` | Wait until each child has ended. This is the default. |
| `join="any"` | Continue when the first child ends. The control plane cancels the other children. |
| `inputs=[keys]` | The keys of the parent's context that each child gets as its starting context. `compile` makes sure that they give each required field of the child's context model. |

A parallel state can have `timeout`. It cannot have `retries`. Declare the child workflows in `flow.py` too. `compile` uses the workflow that no parallel state runs.

### The join

When the join is met, the control plane fires the event `joined` on the parent. Its payload is `{"branches": [...]}`. The list has one entry for each child that ended:

- `workflow`: the child's workflow;
- `token`: the child's id;
- `status`: `completed` or `failed`;
- `final`: the state that the child was in when it ended;
- `payload`: the payload of the event that brought the child into that state.

A guard on `joined` can use `branches.all(...)` and `branches.any(...)`. In the parentheses, use `workflow`, `token`, `status`, `final` and `payload.field`. The guard can also use `ctx` and `count`.

`compile` cannot prove that two guards with `branches` never overlap. So give the guard to one transition, and make the other transition the fallback with `otherwise=True`.

If no transition takes `joined`, the parent fails.

### Child tokens

When the parent enters the state, the control plane starts the children in the same transaction. A child's id is `<parent>.<state>.<visit>.<workflow>`, for example `TICKET-1.reviews.1.design-review`.

A child is a normal token. It has tasks, timers, memory and history. Supervisors claim its tasks as they claim other tasks.

A claim names a child's state as `<workflow>/<state>`, for example `design-review/triage`. In `contract.py`, this state is `states.design_review.triage`. Its events are in `events.design_review`. `stateous init` writes a stub for each state of each workflow, in a file such as `design_review_triage.py`. When two states get the same file name, the second stub goes in `<name>_2.py`, and `init` tells you. A parallel state has no stub.

When a child ends, the control plane checks the join in the same transaction. The parent can leave the state on `joined`, on `timeout` or on an event that you fire. When the parent leaves the state, or when you cancel it, the control plane cancels each child that has not ended.

### Steer the parent

`pause` and `resume` on the parent also pause and resume its children. On resume, the events that the parent and its children held apply in the order that they arrived. Thus, with `join="any"`, the child that ended first goes on. You cannot pause, resume or cancel a child alone: the answer is 409, because a child is a part of its parent's work.

`GET /tokens/{id}` shows a child's `parent`. `GET /tokens?parent=<id>` lists the children. `stateous token show` with the parent's id shows them below the parent.

A child workflow can have parallel states too. `compile` makes sure that a top token id of 64 characters fits below each nesting. If a child's id is longer than 128 characters, the control plane refuses the event that enters the state.

### Work done by tokens that you start yourself

You can also start tokens yourself. A routine starts them with the client, and returns `None`. Each child then reports to the parent with `fire`. You write the join yourself, with guards and `count('event')`.

## Try it on one computer

These steps run the example on one computer, without PHD. You report `coding` yourself. The `testing` routine runs pytest, so install pytest in the same environment. On Windows, use Git Bash.

Run the steps in a copy of this repository, in the environment that has Stateous:

```bash
mkdir -p try/sdlc-supervisor
cp -r examples/sdlc try/
cp examples/sdlc-supervisor/testing.py try/sdlc-supervisor/
cd try
stateous compile sdlc/flow.py
stateous serve sdlc/build/sdlc.json --db sqlite:///try.db
```

In a second terminal, in `try/`:

```bash
stateous init http://127.0.0.1:8080 sdlc-supervisor
rm sdlc-supervisor/coding.py
stateous supervise sdlc-supervisor
```

`init` writes a stub `coding.py`, because no file implements `coding`. You remove it: on this computer, you are the agent.

In a third terminal, in `try/`, make a checkout with one test, and start a token:

```bash
mkdir -p repo
printf 'def test_ok() -> None:\n    assert True\n' > repo/test_ok.py
stateous token start TICKET-1 '{"repo": "repo"}'
stateous token fire TICKET-1 done
stateous token show TICKET-1
```

The supervisor runs the tests, and the token completes. This takes a few seconds. Then `show` prints `TICKET-1: testing, completed`. The path `repo` is relative to the folder where the supervisor runs.

[`tests/e2e/test_example.py`](../tests/e2e/test_example.py) runs the same steps, and more.

## The HTTP API

The API has no prefix. Bodies are JSON. An error has a sentence and a code:

```json
{"error": "task 7 is at attempt 2, not attempt 1", "code": "attempt_over"}
```

The sentence is for people, and it can change. The code does not change: a program reads the code, never the sentence.

| Code | Status | Meaning |
|---|---|---|
| `token_not_found` | 404 | No token has this id. |
| `task_not_found` | 404 | No task has this id. |
| `not_found` | 404 | The API has no such path. |
| `method_not_allowed` | 405 | The path does not take this method. |
| `bad_request` | 400 (or the status of the problem, for example 413 for a body larger than 1 MiB) | The request cannot be read: bad JSON, a bad header. |
| `invalid_argument` | 422 | A supervisor name, a request id, a limit or an attempt is out of range. |
| `invalid_context` | 422 | A context or a context change does not agree with its model. |
| `invalid_payload` | 422 | A payload, a schedule, a note, a memory write or an idempotency key is not valid. |
| `invalid_token_id` | 422 | A token id breaks the pattern, or differs only in case from an existing one. |
| `invalid_body` | 422 | The body does not have the shape of the request. |
| `event_not_allowed` | 409 | The event cannot move the token in its state. |
| `token_not_active` | 409 | The token's status does not allow the request. |
| `child_token` | 409 | The token is a child of a parallel state; steer its top token. |
| `task_not_held` | 409 | The task is not this supervisor's, or no one works on it now. |
| `attempt_over` | 409 | The request is for an attempt of the task that is over: the task moved to a new attempt. |
| `database_busy` | 503 | The database cannot take the request now; try again. |
| `not_serving` | 503 | This control plane no longer holds the database (another one took it). Trying again does not help: stop this server. A supervisor still tries again, because another control plane can take over at the same URL; its log line gives this code. |
| `unexpected` | 500 | The request failed for a reason that no code above names. |

| Method and path | What it does |
|---|---|
| `POST /tokens` | Start a token. Body: `{"token", "context"}`. If the token exists, you get it back (200, else 201). |
| `GET /tokens` | List the tokens. Query: `status`, `parent` (only the children of this token), `limit` (1 to 1000, default 100). |
| `GET /tokens/{id}` | One token: `{"token", "state", "status", "visit", "context", "entered_at"}`. A child also has `"parent"`. |
| `GET /tokens/{id}/history` | The token's history: `[{"seq", "at", "kind", "data"}]`. |
| `POST /tokens/{id}/events/{event}` | Fire an event. Body: `{"payload"}`, optional. Header `Idempotency-Key`, optional. A second delivery with the same key gets `{"duplicate": true, "token": …}`. |
| `POST /tokens/{id}/pause` | Pause the token. |
| `POST /tokens/{id}/resume` | Resume the token. |
| `POST /tokens/{id}/cancel` | Cancel the token. |
| `POST /tasks/claim` | A supervisor takes a task of the states that it serves. Body: `{"supervisor", "states", "request"}`; `request` is optional. A state of a child workflow is `<workflow>/<state>`. 204 if there is no task. |
| `POST /tasks/{id}/report` | The outcome of a task. Body: `{"kind", "event", "payload", "error", "context", "schedules", "attempt"}`; `kind` is `event`, `waiting` or `failed`. An event that the state does not allow is refused. |
| `POST /tasks/heartbeat` | The supervisor's tasks are still alive, in one call. Body: `{"supervisor", "tasks"}`. The answer `{"lost"}` names the lost tasks. |
| `POST /tasks/{id}/note` | A line of progress. Body: `{"text", "attempt"}`. |
| `PUT /tasks/{id}/memory` | Save one key of the routine's memory. Body: `{"key", "value", "attempt"}`. A `null` value deletes the key. |
| `GET /workflow` | The graph file. `init` and `show --graph` use it. |
| `GET /health` | The workflow's name and the graph's checksum: `{"workflow", "checksum"}`. |

A report from outside, for a waiting task, has no `supervisor`. A supervisor's own report adds `"supervisor"`.

`request` in a claim is a request id: 1 to 64 letters, digits, `.`, `_` or `-`. The control plane keeps the last request id of each supervisor, and the task that it got. A claim with the same id gets the same task again, if the supervisor still holds it. Else the claim takes a new task. The supervisor makes a new id after each answer.

`attempt` is optional in a report, a note and a memory write. It names the attempt of the task that the request is for. When the task is on a different attempt, the control plane refuses the request with 409. The code is `attempt_over` when the task moved to a new attempt. So a late report of an old attempt cannot land on a new attempt. The supervisor always sends the attempt of its grant. A request without `attempt`, for example from CI or a person, is for the current attempt.

## The commands

Stateous has five commands.

| Command | What it does |
|---|---|
| `stateous compile FLOW [--out FILE]` | Check the workflow. Write the graph file. |
| `stateous serve GRAPH [--db URL] [--listen HOST:PORT] [--supervise FOLDER]` | The control plane. `--supervise` also runs a supervisor in the same process, for development. |
| `stateous init URL FOLDER [--stateous-path PATH]` | Make a supervisor's folder: `contract.py`, a stub for each state, `stateous.yaml`, `pyproject.toml`. Run it again to update `contract.py`. `--stateous-path` points the folder at a local Stateous checkout. |
| `stateous supervise FOLDER` | The supervisor. |
| `stateous token start ID [CONTEXT_JSON]` | Start a token. |
| `stateous token fire ID EVENT [PAYLOAD_JSON] [--key K]` | Fire an event, with an optional idempotency key. |
| `stateous token show [ID] [--graph]` | Show a token, its children and its history. With no id, list the tokens. `--graph` draws the workflow. |
| `stateous token pause ID` | Pause a token. |
| `stateous token resume ID` | Resume a token. |
| `stateous token cancel ID` | Cancel a token. |

Each `token` command also takes `--url`. An error prints one sentence to stderr, and the exit code is 1. `--help` shows the help of each command.

## What Stateous does not do

- It has no authentication, no tenants and no keys. Add them around Stateous with a proxy, a VPN, or one control plane for each customer.
- It serves one workflow structure for each database. It has no versions side by side.
- It does not launch agents. A routine launches them with PHD.

### Known limits

- `coding.py` needs PHD features that are not released yet. See "Agents, with PHD".
- The supervisor cannot stop a `def` routine. It ignores the result of that routine.
- A paused token never ends on its own. A lease continues while the token is paused.
- `count('event')` counts the event over the whole life of the token, not for one visit.
- In a context or payload model, a field with `default_factory` can be required in `contract.py`. This occurs when the field's type is a model or a union.
- `compile` cannot prove that two guards with `branches.all` or `branches.any` never overlap. Use one such guard and `otherwise=True`.
