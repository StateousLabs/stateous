# Run replicas

This guide tells you how to run several control planes of one workflow at the same time, on one Postgres namespace.

## Before you start

- The workflow's tables are in Postgres. Replicas need Postgres; on SQLite a control plane always serves alone.
- The connection uses psycopg 3, the driver that Stateous ships with: a URL `postgresql://` is enough. An engine of your own must be `postgresql+psycopg://`.
- You run the control plane in your own service. See [Embed a control plane](embed-a-control-plane.md).

## 1. Open each replica with `exclusive=False`

```python
from pathlib import Path

from stateous import ControlPlane


def open_replica(database_url: str) -> ControlPlane:
    """One replica of the `plan` workflow. Each process of the service opens one."""
    return ControlPlane(
        Path("plan/build/plan.json"),
        database=database_url,
        namespace="plan",
        exclusive=False,
    )
```

A replica takes no hold. It writes a row in the table `replicas`, and its clock renews the row. `close()` removes it, and a row that is not renewed for 30 seconds lapses.

An exclusive control plane and replicas do not mix on one namespace. The one that comes second does not start.

## 2. Run the clock of each replica

Run `plane.run_clock()` in each replica, as for one control plane. Each timer and each ended lease runs in its own transaction, under its token's lock. So two clocks never fire one timer twice. The clock also renews the replica's row: a replica with no clock seems gone after 30 seconds.

## 3. Roll out a new graph

- **Only timeouts and retries changed.** The structure is the same, so the new replica starts beside the old ones. It logs a warning, and the newest graph goes on record.
- **States, events or transitions changed.** The structure is new. A replica refuses to start while another replica serves the old structure, or while tokens of the old structure are active or paused. Stop the old replicas first, or use a new namespace.

## 4. Behind a connection pooler

A replica hears the other replicas with Postgres `LISTEN`, which needs a session connection. A pooler in transaction mode, for example PgBouncer, does not give one. So give the replica a direct connection too:

```python
from stateous import ControlPlane


def open_behind_pooler(pooled_url: str, direct_url: str) -> ControlPlane:
    """The calls go through the pooler; LISTEN uses the direct connection."""
    return ControlPlane(
        "plan/build/plan.json",
        database=pooled_url,
        namespace="plan",
        exclusive=False,
        direct=direct_url,
    )
```

## "Task ready" across replicas

When a replica makes a task claimable, it sends a notice with the commit. Postgres sends these notices one commit at a time, so very many replicas that commit at once wait a little for each other. The other replicas hear it, and they wake their waiting claims. A lost listener connection is made again, after 1, 2, 4, 8, 16, then every 30 seconds.

## See also

- [Embed a control plane](embed-a-control-plane.md): `ControlPlane` in your service.
- [Tenancy](../concepts/tenancy.md): labels and the authorizer.
- [Reliability](../concepts/reliability.md): leases, locks and the ledger.
- [Use Postgres](use-postgres.md): the time limits.
