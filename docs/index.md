# Stateous

You write a process as a graph of states. A control plane keeps every token of the process durable. It also chooses each next state. A supervisor, on any computer, does the work and reports what happened.

The supervisor never sees the graph. It knows only which states it serves, and which outcomes each state allows.

## Install

Stateous needs Python 3.12 or newer.

```bash
pip install stateous            # or: uv add stateous
pip install "stateous[phd]"     # also PHD, to run agents in routines
```
