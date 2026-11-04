---
name: stateous
description: Build, change, run and debug a Stateous workflow and its supervisor - flow.py, the schemas, the routines, the compile step and a try-out with the real routines, PHD agents included. Use it when a project uses Stateous, or when the user asks for a workflow, a state machine or a durable process with Stateous.
---

# Stateous

Stateous {version}. This skill is for that version only. The pages in `references/` are the full documentation of that version.

Stateous runs a process as a graph of states. A control plane keeps each token durable and chooses each next state. A supervisor runs your routines: one routine does the work of one state, and returns an event.

## Rules

- Read the page that a step names before you do the step. The pages have the details that this file does not repeat.
- Work from the description that the developer gives. Ask the developer when a choice changes the graph: a state, an event, a guard, or who does the work of a state.
- Never choose an answer for the world. When a state waits for a human, an outside system or a timer, ask the developer what the answer is.
- Run each command with the project's environment, for example `uv run stateous ...` in a uv project.

## Step 1: understand the request

Find the states, the events between them, the context data, and the actors. Find which states a routine does, and which wait for a person or another system.

Read first: `references/concepts/model.md`, `references/concepts/data.md`, `references/concepts/two-sides.md`.

## Step 2: write the workflow

```bash
stateous init workflow NAME
```

This writes `NAME/flow.py`, `NAME/schemas.py` and `NAME/pyproject.toml`, and compiles the starter. Change `flow.py` and `schemas.py` to the developer's workflow.

Read first: `references/how-to/write-a-workflow.md`. As you need them: `references/concepts/guards.md`, `references/concepts/timers.md`, `references/concepts/parallel.md`, `references/concepts/tasks-and-routines.md`.

## Step 3: compile

```bash
stateous compile NAME/flow.py
```

This writes `NAME/build/NAME.json`, the graph file. An error is one sentence. Correct the cause in `flow.py`, and compile again. `references/how-to/write-a-workflow.md` tells how to read each error.

## Step 4: write the routines

```bash
stateous init supervisor NAME/build/NAME.json NAME-supervisor
```

This writes `contract.py`, one stub for each state with a routine, `stateous.yaml` and `pyproject.toml`. Replace the body of each stub with the work of its state. A routine imports `contract.py`, never `flow.py`.

The supervisor's folder is a Python project of its own. Add each package that the routines import to `NAME-supervisor/pyproject.toml`. Then run `uv sync` in that folder.

Read first: `references/how-to/write-a-routine.md`, `references/concepts/tasks-and-routines.md`, `references/concepts/context-notes-memory.md`. For a routine that runs an agent: `references/concepts/agents.md`. For the names and signatures: `references/reference/python.md`.

After each change to `flow.py`, compile again. Then run `stateous init supervisor` again: it writes a new `contract.py`, and a stub for each new state.

## Step 5: try it out

Run the try-out script of this skill with the Python of the supervisor's folder. That Python has Stateous and the packages of the routines:

```text
uv run --project NAME-supervisor python <this skill>/scripts/try_workflow.py NAME/build/NAME.json NAME-supervisor --context @context.json
```

Run it in the folder of the project, so that the paths of the routines stay the same. Write the context in a JSON file, for example `context.json`. A file is safe in each shell. PowerShell changes the quotes of a JSON argument.

The script makes a new SQLite database in a temporary folder. It starts a control plane, the supervisor and one token, and it runs the real routines. It stops by itself and prints a report. Add `--json` for a JSON report.

The options:

- `--context @FILE` or `--context JSON`: the context of the token. It must agree with the context model.
- `--fire STATE=EVENT`, `--fire STATE=EVENT:@FILE` or `--fire STATE=EVENT:PAYLOAD_JSON`: the answer for a state that waits. Give it once for each answer. For a state of a child workflow, write `WORKFLOW/STATE`. The script refuses a state or an event that the graph does not have.
- `--timeout 600`: stop after this many seconds.
- `--quiet 120`: stop when nothing changes for this many seconds. A note or a heartbeat of a running task is a change.
- `--database PATH`: the SQLite file to make.

Exit code 0: the token completed. Exit code 1: the try-out stopped for another reason, or it could not run. Exit code 2: wrong use.

## Step 6: read the report and debug

The first line of the report tells why the try-out stopped. Then the report gives each token with its state and context. It gives each task: its state, its attempt, its time, its event or error, and its notes. Then it gives the full history of each token. The last line names the page to read.

| Stop | What it means | What you do | Page |
|---|---|---|---|
| `completed` | The token ended at END. | Compare the history with what the developer wants. | `references/inside/words.md` |
| `failed` | The token ended at no END, after a task failed. | Read the errors of the steps. Correct the routine, or add a transition on `error`. | `references/concepts/tasks-and-routines.md` |
| `cancelled` | The token was cancelled. | Find what cancelled it. | `references/how-to/steer-tokens.md` |
| `no_routine` | A task waits at a state that no routine serves. | Write its routine. Or ask the developer for the answer, and use `--fire`. | `references/how-to/run-supervisors.md` |
| `waits_outside` | A routine returned no event: the state waits for an event from outside. | Ask the developer for the answer, then run again with `--fire`. | `references/concepts/tasks-and-routines.md` |
| `waits_timer` | The state waits for a timer, or for an event from outside. | Ask the developer. To fire the timer's event early, use `--fire`. | `references/concepts/timers.md` |
| `quiet` | Nothing changed for the time of `--quiet`. | Find the routine that does not move. Add notes to a long routine. | `references/concepts/reliability.md` |
| `time_limit` | The time of `--timeout` ended. The report names the running tasks. | Find why the task takes so long, or give more time. | `references/concepts/agents.md` |
| `fire_refused` | The control plane refused a `--fire`, for example because no guard holds. | Ask the developer for an answer that the workflow takes. | `references/concepts/guards.md` |

An error before the run is one sentence, for example a context that the workflow refuses, or a `contract.py` for another version of the workflow. Correct the cause and run again.

When the version of this skill and the installed Stateous are not the same, the script stops. It names the command that writes the skill again with the right Python. Tell the developer to run it.

When a routine blocks the try-out, the script ends itself some time after `--timeout`, with one sentence.

## Then

To run the workflow for real, run `stateous serve` and `stateous supervise`. Read `references/how-to/run-supervisors.md` and `references/reference/cli.md`. To steer tokens, read `references/how-to/steer-tokens.md`.

## All the reference pages

- `references/index.md`: Stateous, the home page.
- `references/getting-started.md`: Getting started.
- `references/concepts/model.md`: The model.
- `references/concepts/data.md`: Where data goes.
- `references/concepts/two-sides.md`: The two sides.
- `references/concepts/tasks-and-routines.md`: Tasks and routines.
- `references/concepts/guards.md`: Guards.
- `references/concepts/timers.md`: Timers and timeouts.
- `references/concepts/context-notes-memory.md`: Context, notes and memory.
- `references/concepts/pause-resume-cancel.md`: Pause, resume and cancel.
- `references/concepts/parallel.md`: Parallel and nested work.
- `references/concepts/reliability.md`: Reliability.
- `references/concepts/agents.md`: Agents, with PHD.
- `references/concepts/notices.md`: Notices.
- `references/concepts/transports.md`: Transports.
- `references/concepts/tenancy.md`: Tenancy.
- `references/how-to/write-a-workflow.md`: Write a workflow.
- `references/how-to/write-a-routine.md`: Write a routine.
- `references/how-to/use-an-agent.md`: Build a workflow with an agent.
- `references/how-to/run-supervisors.md`: Run supervisors.
- `references/how-to/use-postgres.md`: Use Postgres.
- `references/how-to/steer-tokens.md`: Steer tokens.
- `references/how-to/change-a-workflow.md`: Change a workflow.
- `references/how-to/embed-a-control-plane.md`: Embed a control plane.
- `references/how-to/write-a-transport.md`: Write a transport.
- `references/how-to/run-replicas.md`: Run replicas.
- `references/inside/words.md`: The words inside.
- `references/inside/event-graph.md`: The event graph.
- `references/inside/workflows.md`: The internal workflows.
- `references/inside/data.md`: The data.
- `references/reference/cli.md`: The commands.
- `references/reference/http.md`: The HTTP API.
- `references/reference/python.md`: The Python API, with each signature and docstring.
- `references/reference/files.md`: The files.
- `references/reference/limits.md`: Limits.
