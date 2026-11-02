# Embed a control plane

This guide tells you how to run a control plane inside your own Python service, in place of `stateous serve`.

## Before you start

- You have a graph file from `stateous compile`, for example `plan/build/plan.json`.
- Your service runs Python 3.12 or newer, and it has `stateous` installed.
- For more than one workflow in one database, you use Postgres.

## 1. Make the tables in your deploy

A control plane in your service does not change the database by itself. Run `stateous migrate` in your deploy, before the service starts:

```bash
stateous migrate --db postgresql://app:secret@db.internal:5432/app --namespace plan
```

The namespace is a Postgres schema. It holds the tables of one workflow, apart from the tables of your service. The default namespace is `stateous`.

A control plane that finds no tables, or tables of another version, does not start. Its error names the command to run. From Python, `ControlPlane(..., migrate=True)` makes the tables, and `plane.migrate(notices=True)` turns the notices on.

## 2. Make the control plane

```python
from pathlib import Path

from stateous import ControlPlane


def open_plan(database_url: str) -> ControlPlane:
    """The control plane of the `plan` workflow, in the namespace `plan`."""
    return ControlPlane(Path("plan/build/plan.json"), database=database_url, namespace="plan")
```

`database` is a URL, or a SQLAlchemy engine that your service already has. Stateous uses your engine and never disposes it. An engine must be Postgres; give SQLite as a URL.

On your engine, Stateous sets its statement and lock limits for each of its own transactions. The connect timeout and the keepalives are settings of your engine.

## 3. Run its clock

The clock fires timers and timeouts, and it ends the leases of supervisors that stopped. Run it in your event loop:

```python
import asyncio

from stateous import ControlPlane


async def run(plane: ControlPlane) -> None:
    """Run the clock until the service stops."""
    clock = asyncio.create_task(plane.run_clock())
    try:
        await asyncio.Event().wait()  # your service runs here
    finally:
        plane.close()
        await clock
```

Without an event loop, call `plane.tick()` about once a second.

## 4. Serve its API, or call it directly

To serve the HTTP API, mount the app in your own ASGI app:

```python
from starlette.applications import Starlette
from starlette.routing import Mount

from stateous import ControlPlane
from stateous.control.api import create_app


def app_of(plane: ControlPlane) -> Starlette:
    """Your app, with the Stateous API under /plan."""
    return Starlette(routes=[Mount("/plan", app=create_app(plane))])
```

Or call the control plane from your own code:

```python
from stateous import ControlPlane, Token


def start_plan(plane: ControlPlane, project: str) -> Token:
    """Start the plan of one project. To start it again returns the same token."""
    return plane.start(f"plan-{project}", {"project": project})
```

## 5. Run more than one workflow

Give each workflow its own namespace. One process can hold many control planes:

```python
from pathlib import Path

from stateous import ControlPlane


def open_all(database_url: str) -> dict[str, ControlPlane]:
    """One control plane for each workflow, all in one database."""
    return {
        name: ControlPlane(Path(f"{name}/build/{name}.json"), database=database_url, namespace=name)
        for name in ("plan", "tasks", "build")
    }
```

Each control plane holds its namespace while it runs. A second control plane on the same namespace does not start.

## 6. Connect the workflows

Workflows know nothing about each other. Your code connects them with [notices](../concepts/notices.md): when workflow A emits an event, your handler starts workflow B.

## Connection poolers

The calls of a control plane work through a pooler in transaction mode, for example PgBouncer. The hold of a namespace needs a session connection. So give the control plane a direct connection to Postgres, not one through such a pooler.

## See also

- [Notices](../concepts/notices.md): hear what a workflow emits.
- [Use Postgres](use-postgres.md): the time limits, and one control plane for each namespace.
- [The commands](../reference/cli.md): `stateous migrate`.
- [The Python API](../reference/python.md): `ControlPlane` and its errors.
