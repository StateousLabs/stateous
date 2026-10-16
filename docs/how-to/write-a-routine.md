# Write a routine

This guide tells you how to write the routine of a state. You mark it, return an outcome, and fail when the work fails.

A routine is your function that does the work of one state. It is in the supervisor's folder. See [Tasks and routines](../concepts/tasks-and-routines.md) for what happens around it.

## 1. Make the supervisor's folder

```bash
stateous init supervisor sdlc/flow.py sdlc-supervisor
```

`init` writes `contract.py`, and a stub file for each state that no file of the folder implements. This is the stub for `coding`:

```python
"""The `coding` state of the `sdlc` workflow."""

from stateous import on_state

from contract import TicketContext, events, states


@on_state(states.coding)
def coding(ctx: TicketContext) -> events.done | events.stuck:
    raise NotImplementedError("do the work of 'coding' here")
```

A stub raises `NotImplementedError`, so each task of a stub fails. Replace the body with the work of the state.

## 2. Write the routine

A routine imports only `contract.py` and its own helpers. It never imports `flow.py`.

- Mark the function with `@on_state(states.X)`. Each state has at most one routine in the folder.
- Its argument is `ctx`. The type is the context type of `contract.py`, for example `TicketContext`.
- Its return type names the events that it can return, for example `-> events.passed | events.failed`.

This is the routine of `testing` in the `sdlc` example. It runs pytest in the checkout, and returns `passed` or `failed` with the counts:

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

An event without a payload has no argument, for example `events.done()`. An event with a payload takes the payload model, for example `events.passed(result)`.

## 3. Choose `def` or `async def`

| Routine | Where it runs | When to use it |
|---|---|---|
| `def` | in a thread | Work that blocks, for example `subprocess.run`. |
| `async def` | on the supervisor's event loop | Work that waits a long time, for example an agent. |

When the token leaves the state, the supervisor cancels an `async def` routine at the next heartbeat. It cannot stop a `def` routine, so it ignores the result of that routine. Never block the event loop in an `async def` routine: the heartbeats of all routines stop.

## 4. Return the outcome

A routine returns one of these:

- **An event.** The supervisor reports it, and the token moves.
- **`None`.** The task waits. Someone outside reports the event later, for example a CI job or a person. See [Steer tokens](steer-tokens.md).

This routine starts work outside, and returns `None`:

```python
# coding.py: a person does the work of `coding`, and reports `done` later
from stateous import on_state

from contract import TicketContext, events, states


@on_state(states.coding)
async def code(ctx: TicketContext) -> events.done | events.stuck | None:
    ctx.note(f"waiting for a person to code {ctx['repo']}")
    return None
```

A routine can also write the context with `ctx["key"] = value`, write notes and memory, and set timers. See [Context, notes and memory](../concepts/context-notes-memory.md) and [Timers and timeouts](../concepts/timers.md).

## 5. Fail on purpose

To fail the attempt, `raise` an exception. The supervisor reports the failure with the exception's type and text. The control plane counts one failed attempt:

- When the state has retries left, the task is queued again. The routine runs again, as the next attempt.
- After the last attempt, the token gets the event `error`. If no transition takes `error`, the token fails.

A failed attempt saves none of its context changes. These outcomes also fail the attempt at once, with the reason in the history:

- an event that the state cannot emit;
- an event that no transition takes, because no guard matches;
- context changes that do not agree with the context model;
- a return value that is not an event and not `None`.

A routine can run more than one time for one task. Make its work safe to repeat.

## 6. Turn an agent's answer into an event

An agent gives its answer as JSON. The contract gives each state two helpers for it:

- `states.X.outcome_schema()`: the events that state X can emit, with their payloads, as one JSON schema. Give it to the agent.
- `states.X.outcome(data)`: the event that `data` names, checked against the schema. It raises `OutcomeError` if `data` does not fit.

```python
from stateous import OutcomeError, on_state

from contract import TicketContext, events, states


def ask_the_agent(schema: dict[str, object]) -> object:
    """Your agent call. It returns JSON that fits `schema`."""
    return {"event": "done", "payload": None}


@on_state(states.coding)
def code(ctx: TicketContext) -> events.done | events.stuck:
    answer = ask_the_agent(states.coding.outcome_schema())
    try:
        return states.coding.outcome(answer)
    except OutcomeError:
        return events.stuck()
```

[Agents, with PHD](../concepts/agents.md) shows a full agent routine.

## 7. Run the supervisor

```bash
stateous supervise sdlc-supervisor
```

The supervisor imports each file of the folder, and finds the routines. It does not start when the folder has no routine, or two routines for one state. See [Run supervisors](run-supervisors.md).

## See also

- [The model](../concepts/model.md): the words [routine](../concepts/model.md#routine), [task](../concepts/model.md#task) and [attempt](../concepts/model.md#attempt).
- [Tasks and routines](../concepts/tasks-and-routines.md): claims, leases and retries.
- [Context, notes and memory](../concepts/context-notes-memory.md): every field of `ctx`.
- [Agents, with PHD](../concepts/agents.md): run a coding agent in a routine.
