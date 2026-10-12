# Stateous

Stateous runs a process as a graph of states, and keeps every token of it durable.

You write the graph in Python. A control plane keeps every token and chooses each next state. A supervisor, on any computer, does the work and reports what happened. The supervisor never sees the graph. It knows only which states it serves, and which outcomes each state allows.

> **Status:** pre-release. The API can change.

## Install

Stateous needs Python 3.12 or newer. It is not on PyPI yet. Add it to your project from a copy of this repository:

```bash
uv add --editable path/to/stateous
```

Then run the `stateous` command in that environment, for example with `uv run stateous`. The control plane keeps its data in SQLite or in Postgres.

## The example

[`examples/sdlc/`](examples/sdlc/) is a workflow with two states: `coding` and `testing`. [`examples/sdlc-supervisor/`](examples/sdlc-supervisor/) is the supervisor's folder for it.

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

To try the example on one computer, follow [the guide](docs/guide.md#try-it-on-one-computer).

## Learn more

- [`docs/guide.md`](docs/guide.md): the guide, on one page.
- [`examples/`](examples/): the example.

## Licence

Apache License 2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
