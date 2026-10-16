# Run supervisors

This guide tells you how to run one or more supervisors, on one computer or on many. It also tells you how to stop them safely.

A supervisor claims the tasks of the states that its folder has routines for. See [The two sides](../concepts/two-sides.md).

## 1. Let other computers reach the control plane

By default, `stateous serve` listens on `127.0.0.1:8080`, so only its own computer reaches it. For supervisors on other computers, listen on a wider address:

```bash
stateous serve sdlc/build/sdlc.json --listen 0.0.0.0:8080
```

`serve` prints a warning, because the API has no authentication. Put the control plane behind a VPN or a proxy.

## 2. Make the supervisor's folder

On the supervisor's computer, make the folder from the control plane's URL:

```bash
stateous init supervisor http://cp.internal:8080 sdlc-supervisor
```

`init` gets the graph with `GET /workflow`, and writes `contract.py`. It writes a stub for each state that no file of the folder implements. It writes `stateous.yaml` and `pyproject.toml` only when the folder does not have them.

A folder can implement only some of the states. Remove the stubs of the states that a different supervisor serves. The supervisor claims only the tasks of the states that have a routine in its folder.

## 3. Set `stateous.yaml`

```yaml
url: http://cp.internal:8080   # the control plane
tasks: 4                       # routines at once
name: build-box-1              # optional; the default is the host name
```

- `url` is an `http://` or `https://` address.
- `tasks` is 1 to 1000. It is the number of routines that run at once.
- `name` shows in the token's history, for example `build-box-1 took task 2 (attempt 1)`. Give each supervisor a different name.

## 4. Install the folder's packages

The folder is a Python project of its own. Its `pyproject.toml` depends on Stateous. Add the packages that your routines import to `dependencies`. Then, in the folder:

```bash
uv sync
```

`uv sync` makes the folder's environment, `.venv`. To edit the routines in VS Code, select `.venv` as the Python interpreter. Then VS Code finds `stateous` and `contract.py`.

## 5. Start the supervisor

```bash
stateous supervise sdlc-supervisor
```

The supervisor logs to stderr:

```text
2026-10-06 23:43:39,848 stateous.supervisor: build-box-1 serves testing; the control plane is http://127.0.0.1:8080
2026-10-06 23:43:48,279 stateous.supervisor: task 2: testing of TICKET-1, attempt 1
2026-10-06 23:43:51,708 stateous.supervisor: task 2 of TICKET-1: reported passed
```

`stateous supervise` does not start in these cases:

- `contract.py` is for a different graph. Run `stateous init supervisor` again.
- The folder has no routine, or two routines for one state.
- A file of the folder cannot be imported.
- Another supervisor runs on the same folder. Two supervisors cannot share one outbox.

If the control plane cannot be reached, the supervisor tries again after 1, 2, 4, 8 and 16 seconds, then every 30 seconds. Its routines continue.

## Run more than one supervisor

Start one supervisor for each folder. The folders can be on different computers, and they can serve the same states.

- The control plane gives each task to one supervisor at a time.
- Each supervisor needs its own folder, because each folder has its own outbox.
- To serve only some states on a computer, keep only their routines in its folder.

For example, `coding` runs on a computer with an agent's login, and `testing` runs on a build server. Each computer has a folder with one routine.

## Run a supervisor in the control plane's process

For development, `serve` can also run the supervisor of a folder, in the same process:

```bash
stateous serve sdlc/build/sdlc.json --supervise sdlc-supervisor
```

The supervisor then speaks to the address of this `serve`. The `url` in `stateous.yaml` is not used. Do not use `--supervise` in production: a stop of the control plane also stops the supervisor.

## Stop a supervisor

Press Ctrl-C, or send SIGTERM. The supervisor takes no new tasks, and waits for its running routines:

```text
stopping: no new tasks; waiting for 1 routine(s) (signal again to stop now)
```

A second signal stops it at once, and the exit code is 1. Its running routines are cancelled. Their tasks run again when their leases end, 2 minutes after the last heartbeat.

When the control plane cannot be reached at the stop, the supervisor does not wait for it. The reports stay in the outbox, `.stateous/outbox.db`, and the next `stateous supervise` sends them. See [Reliability](../concepts/reliability.md).

## Try it on one computer

These steps run the `sdlc` example on one computer, without PHD. You report `coding` yourself. The `testing` routine runs pytest, so install pytest in the same environment. On Windows, use Git Bash.

Run the steps in a copy of the Stateous repository, in the environment that has Stateous:

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
stateous init supervisor http://127.0.0.1:8080 sdlc-supervisor
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

The test `tests/e2e/test_example.py` of the repository runs the same steps, and more.

## See also

- [The two sides](../concepts/two-sides.md): what crosses the network.
- [Reliability](../concepts/reliability.md): leases, the outbox, and what happens when a supervisor stops.
- [Write a routine](write-a-routine.md): the routines in the folder.
- [Use Postgres](use-postgres.md): the database of the control plane.
