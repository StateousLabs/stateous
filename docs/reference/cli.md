# The commands

This page lists every `stateous` command, its arguments and its options.

Stateous has seven commands: `compile`, `serve`, `migrate`, `init`, `supervise`, `token` and `install`. `init`, `token` and `install` each have commands of their own.

## For every command

- `--help` shows the help of a command, for example `stateous token fire --help`.
- An error prints one sentence to stderr, as `error: <sentence>`. The exit code is then 1. A command never prints a traceback for an expected error.
- An argument in capital letters is one that you give, for example `ID`. An argument in `[ ]` is optional.

| Command | What it does |
|---|---|
| `stateous compile FLOW` | Check a workflow, and write its graph file. |
| `stateous serve GRAPH` | Run the control plane. |
| `stateous migrate` | Make or update the tables of a database. |
| `stateous init workflow NAME` | Write a starter workflow's folder, and compile it. |
| `stateous init supervisor SOURCE FOLDER` | Write a supervisor's folder. |
| `stateous supervise FOLDER` | Run the supervisor of a folder. |
| `stateous token start ID [CONTEXT_JSON]` | Start a token. |
| `stateous token fire ID EVENT [PAYLOAD_JSON]` | Fire an event on a token. |
| `stateous token show [ID]` | Show a token, or list the tokens. |
| `stateous token pause ID` | Pause a token. |
| `stateous token resume ID` | Resume a token. |
| `stateous token cancel ID` | Cancel a token. |
| `stateous install skill DIRECTORY` | Write the skill for a coding agent. |

## Workflows

### stateous compile

```text
stateous compile FLOW [--out FILE]
```

It loads the workflow in `FLOW`, checks it, and writes the graph file. It prints the path of the graph file, for example `wrote sdlc/build/sdlc.json`.

| Argument or option | Meaning |
|---|---|
| `FLOW` | The workflow's file, for example `sdlc/flow.py`. |
| `--out FILE` | The graph file to write. The default is `build/<workflow>.json`, next to `FLOW`. |

An error names the mistake in one sentence. [Write a workflow](../how-to/write-a-workflow.md) tells how to read it.

```bash
stateous compile sdlc/flow.py
stateous compile sdlc/flow.py --out graphs/sdlc.json
```

### stateous init workflow

```text
stateous init workflow NAME [--stateous-path PATH]
```

It makes the folder `NAME/` with a starter `flow.py`, `schemas.py` and `pyproject.toml`. Then it compiles `flow.py` to `NAME/build/NAME.json`.

| Argument or option | Meaning |
|---|---|
| `NAME` | The workflow's name. It is also the name of the folder. |
| `--stateous-path PATH` | A local Stateous checkout. The folder's `pyproject.toml` then depends on that checkout, to work on Stateous itself. |

It refuses a folder that has a `flow.py`. It keeps a `schemas.py` or a `pyproject.toml` that exists. It prints one line for each file, then the next steps.

```bash
stateous init workflow orders
```

## The control plane

### stateous serve

```text
stateous serve GRAPH [--db URL] [--listen HOST:PORT] [--supervise FOLDER] [--namespace NAME]
```

It opens the database, makes or updates its tables, then starts the HTTP API and the clock. It prints `serving on http://127.0.0.1:8080 (Ctrl-C stops)` to stderr.

| Argument or option | Meaning |
|---|---|
| `GRAPH` | The graph file that `stateous compile` wrote. |
| `--db URL` | The database. The default is `sqlite:///stateous.db`, in the current folder. For Postgres, give a `postgresql://` URL. |
| `--listen HOST:PORT` | The address to listen on. The default is `127.0.0.1:8080`. |
| `--supervise FOLDER` | Also run the supervisor of this folder, in the same process. Use it for development. |
| `--namespace NAME` | Postgres only: the schema that holds the tables. The default is `stateous`. Each workflow in one database has its own namespace. |

An address that other computers can reach gives a warning, because the API has no authentication.

`serve` does not start in these cases. It prints one sentence that tells why.

- Another control plane serves the same database, in the same namespace.
- The graph has a different structure, and the database has active or paused tokens of the old structure.
- The graph file, the database URL or the address is not correct, or the port is in use.

If another control plane takes the database later, `serve` stops with the exit code 1. Ctrl-C stops it, and it prints `stopped`.

```bash
stateous serve sdlc/build/sdlc.json
stateous serve sdlc/build/sdlc.json --db postgresql://stateous:secret@db.internal:5432/stateous --listen 0.0.0.0:8080
stateous serve orders/build/orders.json --supervise orders-supervisor
```

See [Use Postgres](../how-to/use-postgres.md) for `--db`, and [Change a workflow](../how-to/change-a-workflow.md) for a new structure.

### stateous migrate

```text
stateous migrate [--db URL] [--namespace NAME] [--notices | --no-notices]
```

It makes or updates the tables of a workflow's database, and then stops. Run it in a deploy, before your service starts a `ControlPlane`. It prints one line, for example `migrated sqlite:///stateous.db to 0001; notices off`.

| Argument or option | Meaning |
|---|---|
| `--db URL` | The database. The default is `sqlite:///stateous.db`, in the current folder. |
| `--namespace NAME` | Postgres only: the schema that holds the tables. The default is `stateous`. |
| `--notices`, `--no-notices` | Turn the notices of the database on or off. Without the option, the setting does not change. |

`stateous serve` makes or updates the tables by itself. A `ControlPlane` in your own service does not: it refuses tables that are missing or out of date, and names this command.

```bash
stateous migrate --db postgresql://stateous:secret@db.internal:5432/app --namespace plan_tasks --notices
```

## Supervisors

### stateous init supervisor

```text
stateous init supervisor SOURCE FOLDER [--stateous-path PATH]
```

It writes a supervisor's folder. [The files](files.md) tells what each file holds.

| Argument or option | Meaning |
|---|---|
| `SOURCE` | The workflow: a control plane's URL, a graph file, or a `flow.py`. |
| `FOLDER` | The supervisor's folder. It is made if it does not exist. |
| `--stateous-path PATH` | A local Stateous checkout. The folder's `pyproject.toml` then depends on that checkout, to work on Stateous itself. |

- A URL starts with `http://` or `https://`. The command gets the graph from `GET /workflow`.
- A `flow.py` is compiled in memory. No graph file is written.
- With a graph file or a `flow.py`, `stateous.yaml` gets `http://127.0.0.1:8080`, the default address of `serve`.

It always writes `contract.py`. It writes a stub only for a state that no file implements. It writes `stateous.yaml` and `pyproject.toml` only when they do not exist. Run it again after each change to the workflow.

It prints one line for each file: `wrote`, `kept` or `skipped`. For example:

```text
wrote    sdlc-supervisor/contract.py
wrote    sdlc-supervisor/coding.py
kept     the routine of 'testing'
wrote    sdlc-supervisor/stateous.yaml
wrote    sdlc-supervisor/pyproject.toml

Write the routines, then run: stateous supervise sdlc-supervisor
```

```bash
stateous init supervisor http://cp.internal:8080 sdlc-supervisor
stateous init supervisor sdlc/build/sdlc.json sdlc-supervisor
stateous init supervisor sdlc/flow.py sdlc-supervisor --stateous-path ../stateous
```

### stateous supervise

```text
stateous supervise FOLDER
```

It runs the routines of the folder. It takes tasks of the states that its routines serve, and it reports their outcomes. It logs to stderr.

| Argument | Meaning |
|---|---|
| `FOLDER` | The supervisor's folder. |

It has no options: its settings are in `stateous.yaml`. Ctrl-C or SIGTERM once stops new claims, and it waits for the running routines. A second signal cancels them, and the exit code is 1.

It does not start in these cases:

- `contract.py` is for a different graph. Run `stateous init supervisor` again.
- The folder has no routine, or two routines for one state.
- A file of the folder cannot be imported.
- Another supervisor runs on the same folder.

```bash
stateous supervise sdlc-supervisor
```

See [Run supervisors](../how-to/run-supervisors.md).

## Tokens

Each `token` command sends one request to the control plane. Each one takes `--url`, the control plane's URL. The default is the environment variable `STATEOUS_URL`, else `http://127.0.0.1:8080`.

A token's line has the form `TICKET-1: coding, active`. From the second visit to a state, it adds `(visit 2)` after the state.

### stateous token start

```text
stateous token start ID [CONTEXT_JSON] [--label KEY=VALUE ...] [--url URL]
```

It starts a token, and prints its line. If the token exists, it shows that token and does not start a second one.

| Argument or option | Meaning |
|---|---|
| `ID` | The token's id, for example `TICKET-1`. |
| `CONTEXT_JSON` | The token's context, as a JSON object. The default is `{}`. |
| `--label KEY=VALUE` | A label of the token, for example `user=u42`. Give it once for each label. Labels never change after the start. |
| `--url URL` | The control plane's URL. |

```bash
stateous token start TICKET-1 '{"repo": "acme/api"}'
```

### stateous token fire

```text
stateous token fire ID EVENT [PAYLOAD_JSON] [--key K] [--url URL]
```

It fires an event on a token, and prints the token's line. A second delivery with the same key prints `duplicate:` before the line.

| Argument or option | Meaning |
|---|---|
| `ID` | The token's id. |
| `EVENT` | The event's name. |
| `PAYLOAD_JSON` | The event's payload, as a JSON object. |
| `--key K` | An idempotency key, from 1 to 256 characters. The same key fires one time. |
| `--url URL` | The control plane's URL. |

```bash
stateous token fire TICKET-1 failed '{"passed": 3, "failed": 1}' --key gh-delivery-81231
```

### stateous token show

```text
stateous token show [ID] [--graph] [--json] [--url URL]
```

With an `ID`, it prints the token's line, its context, its children and its history. With no `ID`, it lists at most 100 tokens, the most recently moved first.

| Argument or option | Meaning |
|---|---|
| `ID` | A token. Leave it out to list the tokens. |
| `--graph` | Draw the workflow as a Mermaid diagram. Do not give an `ID` with it. |
| `--json` | Print the token, its children and its history as one JSON object, for scripts. It needs an `ID`. |
| `--url URL` | The control plane's URL. |

It shows at most 1000 children. Each history line has the local time, the sequence number and what happened.

```bash
stateous token show TICKET-1
stateous token show TICKET-1 --json
stateous token show --graph
```

### stateous token pause

```text
stateous token pause ID [--url URL]
```

It pauses an active token: its events are held, its tasks wait, and its timers stand still.

| Argument or option | Meaning |
|---|---|
| `ID` | The token's id. |
| `--url URL` | The control plane's URL. |

```bash
stateous token pause TICKET-1
```

### stateous token resume

```text
stateous token resume ID [--url URL]
```

It resumes a paused token. The held events apply in the order that they arrived.

| Argument or option | Meaning |
|---|---|
| `ID` | The token's id. |
| `--url URL` | The control plane's URL. |

```bash
stateous token resume TICKET-1
```

### stateous token cancel

```text
stateous token cancel ID [--url URL]
```

It cancels an active or a paused token for good.

| Argument or option | Meaning |
|---|---|
| `ID` | The token's id. |
| `--url URL` | The control plane's URL. |

```bash
stateous token cancel TICKET-1
```

You cannot pause, resume or cancel a child token. Steer its top token. See [Steer tokens](../how-to/steer-tokens.md).

## Tooling

### stateous install skill

```text
stateous install skill DIRECTORY [--force]
```

It writes the Stateous skill for a coding agent into `DIRECTORY/stateous/`. The skill has `SKILL.md`, the pages of this site in `references/`, and a try-out script in `scripts/`. It is for the version of Stateous that is installed. It prints the path that it wrote.

| Argument or option | Meaning |
|---|---|
| `DIRECTORY` | The folder where your coding agent reads skills. You choose it. The command makes it if it does not exist. |
| `--force` | Replace the skill if it exists. Use it after you upgrade Stateous. |

It refuses a `DIRECTORY/stateous/` that exists, if you do not give `--force`. See [Build a workflow with an agent](../how-to/use-an-agent.md).

```bash
stateous install skill .claude/skills
stateous install skill .claude/skills --force
```

## See also

- [The HTTP API](http.md): the request that each `token` command sends.
- [The files](files.md): the files that `compile` and `init` write.
- [Steer tokens](../how-to/steer-tokens.md): the `token` commands, step by step.
