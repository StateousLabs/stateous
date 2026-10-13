# Stateous

Stateous runs a process as a graph of states, and keeps every token of it durable.

You write the graph in Python. A control plane keeps every token and chooses each next state. A supervisor, on any computer, does the work and reports what happened. The supervisor never sees the graph. It knows only which states it serves, and which outcomes each state allows.

> **Status:** alpha. The API can change before version 1.0.

## Install

Stateous needs Python 3.12 or newer.

```bash
pip install stateous            # or: uv add stateous
pip install "stateous[phd]"     # also PHD, to run agents in routines
```

Then run the `stateous` command in that environment. The control plane keeps its data in SQLite or in Postgres.

## The example

[`examples/sdlc/`](https://github.com/StateousLabs/stateous/tree/main/examples/sdlc) is a workflow with two states: `coding` and `testing`. [`examples/sdlc-supervisor/`](https://github.com/StateousLabs/stateous/tree/main/examples/sdlc-supervisor) is the supervisor's folder for it.

```bash
# on the control plane's computer
stateous compile sdlc/flow.py                         # writes sdlc/build/sdlc.json
stateous serve sdlc/build/sdlc.json --listen 0.0.0.0:8080

# on the supervisor's computer
stateous init http://cp.internal:8080 sdlc-supervisor # contract.py, stubs, stateous.yaml, pyproject.toml
stateous supervise sdlc-supervisor                    # runs the routines

# anywhere
stateous token start TICKET-1 '{"repo": "acme/api"}' --url http://cp.internal:8080
stateous token show TICKET-1 --url http://cp.internal:8080
```

Stateous has no authentication. Keep a control plane that listens on a wider address behind a VPN or a proxy.

To try the example on one computer, follow [the guide](https://github.com/StateousLabs/stateous/blob/main/docs/guide.md#try-it-on-one-computer).

## Learn more

- [`docs/guide.md`](https://github.com/StateousLabs/stateous/blob/main/docs/guide.md): the guide, on one page.
- [`examples/`](https://github.com/StateousLabs/stateous/tree/main/examples): the example.

## Licence

Apache License 2.0. See [`LICENSE`](https://github.com/StateousLabs/stateous/blob/main/LICENSE) and [`NOTICE`](https://github.com/StateousLabs/stateous/blob/main/NOTICE).
