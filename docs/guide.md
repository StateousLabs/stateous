# The Stateous guide

This page tells you how to use Stateous. It uses one example from start to end: [`examples/sdlc/`](../examples/sdlc/) and [`examples/sdlc-worker/`](../examples/sdlc-worker/). The design is in [`design.md`](design.md).

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

sdlc-worker/             the supervisor's side, on any computer: no graph
  contract.py            written by `stateous init`: states, their events, the models
  coding.py              @on_state(states.coding) ...
  testing.py             @on_state(states.testing) ...
  stateous.yaml          the control plane's URL, how many tasks at once
```

The supervisor's folder never holds `flow.py` or the graph file. Its routines import only `contract.py`.

## Install

Stateous needs Python 3.11 or newer. It is not on PyPI yet. Add it to your project from a copy of its repository:

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
| `flow.event(name, payload=Model)` | An event. `payload` is optional. |
| `flow.transition(source, target, on=event, when=None, otherwise=False)` | Where an event goes. `START` and `END` begin and end every token. |

Rules:

- A transition from `START` has no `on=`.
- A name of a state or an event has lower-case letters, digits, `_` and `-`. It starts with a letter and has 64 characters or fewer.
- A duration is a whole number and one unit: `30s`, `10m`, `6h` or `2d`. The longest is `3650d`.
- Stateous has two events of its own: `timeout` and `error`. Use them by name, for example `on="timeout"`. You cannot declare them, and you cannot fire them.
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
stateous init http://cp.internal:8080 sdlc-worker
```

`stateous init` gets the graph from the control plane. Then it writes these files:

- `contract.py`, always (it replaces the old file);
- a stub `<state>.py` for each state that no file in the folder implements;
- `stateous.yaml`, only if the folder does not have it.

`init` never writes over a routine file or over `stateous.yaml`. A stub that would hide a Python module gets the name `<state>_routine.py`. For example, the state `queue` gets `queue_routine.py`. `init` prints one line for each file:

```text
wrote    sdlc-worker\contract.py
wrote    sdlc-worker\coding.py
kept     the routine of 'testing'
wrote    sdlc-worker\stateous.yaml

Write the routines, then run: stateous supervise sdlc-worker
```

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
# sdlc-worker/testing.py
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
| `ctx.note(text)` | One line of progress in the token's history: 1 to 500 characters. It is sent at once. Empty text raises `ValueError`. |
| `ctx.token` | The token's id. |
| `ctx.task` | The task's id. |
| `ctx.visit` | 1 the first time the token is in this state, 2 the next time, and so on. |
| `ctx.attempt` | 1, then 2 after a retry. |
| `ctx.entered_by` | The event that brought the token here: `.name` and `.payload`. |
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
stateous supervise sdlc-worker
```

The supervisor runs at most `tasks` routines at once. An `async def` routine runs on the supervisor's event loop. A `def` routine runs in a thread. While a routine runs, the supervisor sends a heartbeat every 30 seconds.

When the token leaves the state, the supervisor cancels an `async def` routine. It cannot stop a `def` routine, so it ignores the result of that routine.

The supervisor logs to stderr:

```text
2026-10-06 23:43:39,848 stateous.supervisor: MorsyB serves testing; the control plane is http://127.0.0.1:8080
2026-10-06 23:43:48,279 stateous.supervisor: task 2: testing of TICKET-1, attempt 1
2026-10-06 23:43:51,708 stateous.supervisor: task 2 of TICKET-1: reported passed
```

To stop the supervisor, press Ctrl-C or send SIGTERM. It takes no new tasks, and it waits for its running routines. A second signal stops it at once, with the exit code 1.

If the supervisor cannot reach the control plane, its routines continue. It tries again after 1, 2, 4, 8 and 16 seconds, then every 30 seconds.

`stateous supervise` does not start in these cases:

- `contract.py` is for a different graph. Run `stateous init` again.
- The folder has no routine, or two routines for one state.
- A file of the folder cannot be imported.

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

A routine that returns `None` leaves its task waiting. The outside actor then calls `c.report(task, event, payload)` with the task's id. The client raises `Refused` (from `stateous.client`) when the control plane refuses a call. `Refused` has `.status` and `.message`. It raises `Unreachable` when the control plane does not answer.

## Agents, with PHD

PHD is a library that runs coding agents. A routine calls PHD, and the supervisor watches the routine. PHD does not report to Stateous. It knows nothing of workflows.

```python
# sdlc-worker/coding.py: an agent, run by PHD (see the guide, "Agents, with PHD")
import phd

from stateous import on_state

from contract import TicketContext, events, states


@on_state(states.coding)
async def code(ctx: TicketContext) -> events.done | events.stuck:
    spec = phd.Spec(
        harness="claude",
        model="sonnet",
        cwd=ctx["repo"],
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
        raise RuntimeError(result.error)  # a failed attempt
    return states.coding.outcome(result.structured_output)
```

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

`coding.py` gives each `event.summary` to `ctx.note(...)`. An empty summary makes `ctx.note` raise. Then the attempt fails.

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
        cwd=ctx["repo"],
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
- **A crash** (or `kill -9`): today, PHD runs each agent in its own process group. So the agent does not stop with the supervisor. It can change the repository and spend money until it next writes output.
- PHD batch 1 ties the agent to its caller: a Windows Job Object, `PR_SET_PDEATHSIG` on Linux, a watchdog on macOS. After that, the agents stop with the supervisor.
- The leases of a dead supervisor end after 2 minutes. Then the tasks are tried again. With `ctx.memory`, the retry resumes the agent's session.
- **Steps with side effects:** the agent can repeat its last step or two. The agent's instructions must make that safe, for example "check whether the PR exists first". Stateous cannot do this for the agent.

## Reliability

Six mechanisms keep tokens safe:

| Mechanism | What it protects |
|---|---|
| **The ledger** | Each change adds a row to one table. Tokens, tasks and timers change in the same transaction. A task is never lost between "the token moved" and "the task was queued". |
| **Leases** | A claimed task has a lease of 2 minutes. The supervisor's heartbeat renews it while the routine runs. When a supervisor dies, its leases end, and the tasks are tried again. |
| **The clock** | It fires timeouts and timers. A timeout is a timer that the state sets when a token enters it. |
| **Idempotency keys** | A webhook that comes two times is applied one time. The second delivery with the same key gets the answer "duplicate". |
| **The routine's memory** | An agent's session stays after a crash and a retry. |
| **Pause and resume** | A paused token does not move. Its events wait, its tasks wait, and its timers stop. On resume, the waiting events apply in order. Events that no longer fit are refused and recorded. |

Also, one token moves one step at a time, because each change locks the token. One control plane serves one database. A second control plane does not start.

An ended lease counts as a failed attempt. The history tells why, for example `task 2 failed: build-box-1 stopped answering: no heartbeat for 2 minutes`.

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
- A routine that runs already continues, and its lease continues too. The control plane holds its outcome.
- On resume, the control plane drops a held outcome if the token left that state meanwhile. The history records `dropped on resume`.
- A task that fails while the token is paused holds the event `error` until the resume.

Only an active token can be paused, and only a paused token can be resumed. `cancel` works on an active or a paused token. A cancelled token takes no more events.

## Nested and parallel work

The engine has no nested states and no parallel states. But the work of a state can be other workflows. Use this pattern:

1. The routine of `reviews` starts child tokens with the client. Then it returns `None`.
2. Each child is a token of a review workflow, on its own control plane. In its last state, the child fires its verdict on the parent.
3. The parent joins with a guard.

```python
# The parent's workflow (part): the join
reviews = flow.state("reviews")
approved = flow.event("approved")
rejected = flow.event("rejected")

flow.transition(coding, reviews, on=done)
flow.transition(reviews, testing, on=approved, when="count('approved') >= 2")
flow.transition(reviews, reviews, on=approved, otherwise=True)  # the first approval: wait
flow.transition(reviews, coding, on=rejected)
```

The first approval goes from `reviews` back to `reviews`. That transition enters the state again, so the control plane makes a new task, and the routine runs again. The routine must not start the children a second time. So it keeps a mark in `ctx.memory`, which stays across visits to the state:

```python
# The parent's routine: start the reviews once, then wait
from stateous import Client, on_state

from contract import TicketContext, states

REVIEWS = "http://reviews.internal:8080"  # the review workflow's control plane


@on_state(states.reviews)
def reviews(ctx: TicketContext) -> None:
    if "children" in ctx.memory:
        return None  # an approval brought the token back: wait for the next verdict
    with Client(REVIEWS) as client:
        for kind in ("security", "design"):
            client.start(f"{ctx.token}-{kind}", {"parent": ctx.token, "kind": kind})
    ctx.memory["children"] = ["security", "design"]
    return None  # the children report later
```

```python
# The child's routine, in the review workflow's supervisor folder
from stateous import Client, on_state
from stateous.client import Refused

from contract import ReviewContext, events, states

PARENT = "http://cp.internal:8080"  # the parent's control plane


@on_state(states.verdict)
def verdict(ctx: ReviewContext) -> events.sent:
    name = "approved" if ctx["findings"] == 0 else "rejected"
    try:
        with Client(PARENT) as parent:
            parent.fire(ctx["parent"], name, key=f"{ctx.token}-verdict")
    except Refused:
        pass  # the parent left `reviews` already: it needs no more verdicts
    return events.sent()
```

`client.start` with an id that exists returns that token. So if the parent's routine crashes before it writes the mark, the retry starts no second child.

The key `f"{ctx.token}-verdict"` makes the verdict apply one time, also when the child's routine runs again.

States, events, guards with counts, and tokens that start other tokens give sequences, branches, loops, retries, fan-out and joins.

The pattern has limits:

- Cancelling the parent does not cancel the children.
- You write the join by hand.
- `count('approved')` counts all approvals of the token. So this join is correct only when the token enters `reviews` from `coding` once.
- For the same reason, the mark in `ctx.memory` stays when the token comes back from `coding`. The routine then starts no new children.

## Try it on one computer

These steps run the example on one computer, without PHD. You report `coding` yourself. The `testing` routine runs pytest, so install pytest in the same environment. On Windows, use Git Bash.

Run the steps in a copy of this repository, in the environment that has Stateous:

```bash
mkdir -p try/sdlc-worker
cp -r examples/sdlc try/
cp examples/sdlc-worker/testing.py try/sdlc-worker/
cd try
stateous compile sdlc/flow.py
stateous serve sdlc/build/sdlc.json --db sqlite:///try.db
```

In a second terminal, in `try/`:

```bash
stateous init http://127.0.0.1:8080 sdlc-worker
rm sdlc-worker/coding.py
stateous supervise sdlc-worker
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

The API has no prefix. Bodies are JSON. An error is `{"error": "<one sentence>"}`, with one of these statuses:

- 400: the body is not JSON;
- 404: there is no such token, task or path;
- 409: the token or the task cannot do this now, for example an event that the state does not allow;
- 405: the path does not take this method;
- 413: the body is larger than 1 MiB;
- 422: a value is not correct, for example a context that does not agree with its model;
- 500: an unexpected failure;
- 503: the database cannot take the request now.

| Method and path | What it does |
|---|---|
| `POST /tokens` | Start a token. Body: `{"token", "context"}`. If the token exists, you get it back (200, else 201). |
| `GET /tokens` | List the tokens. Query: `status`, `limit` (1 to 1000, default 100). |
| `GET /tokens/{id}` | One token: `{"token", "state", "status", "visit", "context", "entered_at"}`. |
| `GET /tokens/{id}/history` | The token's history: `[{"seq", "at", "kind", "data"}]`. |
| `POST /tokens/{id}/events/{event}` | Fire an event. Body: `{"payload"}`, optional. Header `Idempotency-Key`, optional. A second delivery with the same key gets `{"duplicate": true, "token": …}`. |
| `POST /tokens/{id}/pause` | Pause the token. |
| `POST /tokens/{id}/resume` | Resume the token. |
| `POST /tokens/{id}/cancel` | Cancel the token. |
| `POST /tasks/claim` | A supervisor takes a task of the states that it serves. Body: `{"supervisor", "states"}`. 204 if there is no task. |
| `POST /tasks/{id}/report` | The outcome of a task. Body: `{"kind", "event", "payload", "error", "context", "schedules"}`; `kind` is `event`, `waiting` or `failed`. An event that the state does not allow is refused. |
| `POST /tasks/heartbeat` | The supervisor's tasks are still alive, in one call. Body: `{"supervisor", "tasks"}`. The answer `{"lost"}` names the lost tasks. |
| `POST /tasks/{id}/note` | A line of progress. Body: `{"text"}`. |
| `PUT /tasks/{id}/memory` | Save one key of the routine's memory. Body: `{"key", "value"}`. A `null` value deletes the key. |
| `GET /workflow` | The graph file. `init` and `show --graph` use it. |
| `GET /health` | The workflow's name and the graph's checksum: `{"workflow", "checksum"}`. |

A report from outside, for a waiting task, has no `supervisor`. A supervisor's own report adds `"supervisor"`.

## The commands

Stateous has five commands.

| Command | What it does |
|---|---|
| `stateous compile FLOW [--out FILE]` | Check the workflow. Write the graph file. |
| `stateous serve GRAPH [--db URL] [--listen HOST:PORT] [--supervise FOLDER]` | The control plane. `--supervise` also runs a supervisor in the same process, for development. |
| `stateous init URL FOLDER` | Make a supervisor's folder: `contract.py`, a stub for each state, `stateous.yaml`. Run it again to update `contract.py`. |
| `stateous supervise FOLDER` | The supervisor. |
| `stateous token start ID [CONTEXT_JSON]` | Start a token. |
| `stateous token fire ID EVENT [PAYLOAD_JSON] [--key K]` | Fire an event, with an optional idempotency key. |
| `stateous token show [ID] [--graph]` | Show a token. With no id, list the tokens. `--graph` draws the workflow. |
| `stateous token pause ID` | Pause a token. |
| `stateous token resume ID` | Resume a token. |
| `stateous token cancel ID` | Cancel a token. |

Each `token` command also takes `--url`. An error prints one sentence to stderr, and the exit code is 1. `--help` shows the help of each command.

## What Stateous does not do

- It has no authentication, no tenants and no keys. Add them around Stateous with a proxy, a VPN, or one control plane for each customer.
- It has no nested or parallel states in the engine. Use the pattern in "Nested and parallel work".
- It serves one workflow structure for each database. It has no versions side by side.
- It does not launch agents. A routine launches them with PHD.

### Known limits

- `coding.py` needs PHD features that are not released yet. See "Agents, with PHD".
- An empty `event.summary` from PHD makes `ctx.note` raise, and the attempt fails.
- The supervisor cannot stop a `def` routine. It ignores the result of that routine.
- A paused token never ends on its own. A lease continues while the token is paused.
- `count('event')` counts the event over the whole life of the token, not for one visit.
- In a context or payload model, a field with `default_factory` can be required in `contract.py`. This occurs when the field's type is a model or a union.
