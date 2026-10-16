# Change a workflow

This guide tells you how to put a changed workflow into service, also when tokens of the old workflow are still live.

A database has one workflow structure at a time. Stateous has no versions side by side.

## What is a change of structure

The structure is the graph without the operational settings of its states. These changes are changes of structure:

- a new state, event or transition, or one that you remove;
- a changed guard, target, payload model or context model;
- a change to a parallel state: its workflows, `join` or `inputs`.

A change to only `timeout=` or `retries=` is not a change of structure. It changes only how the work is done.

## 1. Change and compile

Edit `flow.py` and `schemas.py`. Then compile again:

```bash
stateous compile sdlc/flow.py
```

If `compile` finds a mistake, it writes no graph file. See [Write a workflow](write-a-workflow.md).

## 2. Serve the new graph file

Stop the control plane with Ctrl-C. Then serve the new graph file on the same database:

```bash
stateous serve sdlc/build/sdlc.json
```

At start, `serve` compares the structure of the graph file with the structure on record in the database.

| The database has | The graph file has | `serve` |
|---|---|---|
| no graph on record | any graph | Starts, and puts the graph on record. |
| the same structure | the same or other `timeout=` and `retries=` | Starts, and puts the new graph on record. |
| a different structure, and no live tokens | a new structure | Starts, and puts the new graph on record. |
| a different structure, and live tokens | a new structure | Does not start. |

Live tokens are tokens that are active or paused. A token that completed, failed or was cancelled is not live. Its history stays in the database.

When `serve` does not start, it prints one sentence, and the exit code is 1:

```text
error: refusing to serve: 1 token(s) are active or paused on another structure of the workflow 'sdlc'; serve the graph file they started on until they end, or cancel them
```

Then do one of these:

- Serve the old graph file until the live tokens end. Then serve the new one.
- Cancel the live tokens with `stateous token cancel`, then serve the new graph file.
- Serve the new graph file on a new database, with `--db`. The old tokens stay in the old database.

A new `timeout=` applies when a token next enters the state. A timer that is set already keeps its time. A new `retries=` applies at the next failed attempt.

## 3. Update each supervisor's folder

Each change to the graph file changes its checksum, also a change to only `timeout=` or `retries=`. A supervisor does not start when its `contract.py` is for a different checksum:

```text
error: /srv/sdlc-supervisor/contract.py is for another version of the workflow (sha256:...; the control plane serves sha256:...): run `stateous init supervisor http://127.0.0.1:8080 /srv/sdlc-supervisor` again
```

So, for each supervisor, stop it, and run `init` again on its folder:

```bash
stateous init supervisor http://127.0.0.1:8080 sdlc-supervisor
stateous supervise sdlc-supervisor
```

`init` replaces `contract.py`. It writes a stub for each new state that no file of the folder implements. It never writes over a routine file, `stateous.yaml` or `pyproject.toml`.

A routine whose state or event you removed does not import with the new `contract.py`. Then `stateous supervise` does not start, and prints the import error. Remove the routine, or change it.

A supervisor that runs checks its contract only at start. Restart it after each change, so that its contract and the control plane agree.

## See also

- [Write a workflow](write-a-workflow.md): compile errors and how to read them.
- [Run supervisors](run-supervisors.md): stop and start a supervisor.
- [Steer tokens](steer-tokens.md): cancel the live tokens.
- [The two sides](../concepts/two-sides.md): the contract and its checksum.
