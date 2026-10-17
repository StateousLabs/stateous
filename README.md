# Stateous

[![PyPI](https://img.shields.io/pypi/v/stateous?cacheSeconds=3600)](https://pypi.org/project/stateous/)
[![Python](https://img.shields.io/pypi/pyversions/stateous?cacheSeconds=3600)](https://pypi.org/project/stateous/)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-blue)](https://stateouslabs.github.io/stateous/)
[![License](https://img.shields.io/pypi/l/stateous?cacheSeconds=3600)](https://github.com/StateousLabs/stateous/blob/main/LICENSE)

Stateous runs a process as a graph of states, and keeps every token of it durable.

You write the graph in Python. A control plane keeps every token and chooses each next state. A supervisor, on any computer, does the work and reports what happened. The supervisor never sees the graph. It knows only which states it serves, and which outcomes each state allows.

> **Status:** alpha. The API can change before version 1.0.

## Install

Stateous needs Python 3.12 or newer.

```bash
pip install stateous            # or: uv add stateous
pip install "stateous[phd]"     # also PHD, to run agents in routines
```

The control plane keeps its data in SQLite or in Postgres.

## A short example

```bash
stateous init workflow orders                               # a starter workflow that compiles
stateous serve orders/build/orders.json                     # the control plane
stateous init supervisor orders/flow.py orders-supervisor   # contract.py and a stub for each state
stateous supervise orders-supervisor                        # runs your routines
stateous token start ORDER-1 '{"title": "first order"}'     # starts a token
```

Write the routines in `orders-supervisor/` before you run `supervise`. [`examples/sdlc/`](https://github.com/StateousLabs/stateous/tree/main/examples/sdlc) and [`examples/sdlc-supervisor/`](https://github.com/StateousLabs/stateous/tree/main/examples/sdlc-supervisor) are a larger example.

Stateous has no authentication. Keep a control plane that listens on a wider address behind a VPN or a proxy.

## Documentation

The documentation is at [stateouslabs.github.io/stateous](https://stateouslabs.github.io/stateous/). It explains the model, then tells you how to do the work, then gives the reference. To run the short example step by step, follow [Getting started](https://stateouslabs.github.io/stateous/getting-started/).

## Licence

Apache License 2.0. See [`LICENSE`](https://github.com/StateousLabs/stateous/blob/main/LICENSE) and [`NOTICE`](https://github.com/StateousLabs/stateous/blob/main/NOTICE).
