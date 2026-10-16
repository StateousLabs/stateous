# Use Postgres

This guide tells you how to keep the control plane's data in Postgres in place of SQLite, and what the time limits do.

## Before you start

- You have a Postgres server and an empty database, or a database that has no tables with the names that Stateous uses.
- Stateous has its Postgres driver, psycopg 3. You do not install it yourself.

## 1. Give the database URL to `serve`

```bash
stateous serve sdlc/build/sdlc.json --db postgresql://stateous:secret@db.internal:5432/stateous
```

`--db` takes a URL that starts with `postgresql://` or `postgres://`. Without `--db`, the database is `sqlite:///stateous.db` in the current folder.

At the first start, `serve` makes its tables in the database. It keeps its schema version in its own table, `stateous_version`. So a different application can use Alembic in the same database.

`serve` does not start when the database has tables with the names that Stateous uses, but no version of Stateous. It also does not start when a different version of Stateous made the database. Then use a new database.

## 2. Run one control plane for each database

One control plane serves one database. A second `stateous serve` on the same database does not start:

```text
error: another control plane serves this database; stop it first
```

On Postgres, the control plane holds a lock in the database while it serves. When its process stops, the lock ends with its connection. Then a new control plane can start at once.

If a different control plane takes the database later, the first one stops with the exit code 1. Each of its requests gets 503, with the code `not_serving`.

The supervisors do not know which database the control plane uses. They need only its URL.

## 3. Know the time limits

Each Postgres connection has time limits, so a request does not wait for ever:

| Limit | Value | What it stops |
|---|---|---|
| `connect_timeout` | 10 seconds | A connection that cannot be made. |
| `statement_timeout` | 30 seconds | One statement that runs too long. |
| `lock_timeout` | 10 seconds | A statement that waits too long for a lock, also a token's lock. |
| TCP keepalives | a probe after 30 seconds, then every 10 seconds, 3 probes | A dead connection. It is found in about a minute. |

When a limit stops a request, the answer is 503 with the code `database_busy`. Send the request again. A supervisor tries again by itself.

When Postgres stops a transaction for a deadlock, the control plane runs it one more time. A schema upgrade at start has no statement limit, but it keeps the lock limit.

## SQLite or Postgres

| | SQLite | Postgres |
|---|---|---|
| `--db` | `sqlite:///stateous.db` (the default) | `postgresql://user:password@host:5432/name` |
| Where the data is | one file on the control plane's computer | a database server |
| Writers at once | one; a transaction waits up to 30 seconds for the write lock | many; each token has its own lock |
| When a control plane stops without a clean end | a new one can start after 30 seconds | a new one can start at once |

Each token moves one step at a time on both databases.

## See also

- [Reliability](../concepts/reliability.md): the ledger, and the time limits among the other mechanisms.
- [Run supervisors](run-supervisors.md): supervisors on other computers.
- [Change a workflow](change-a-workflow.md): what `serve` checks in the database at start.
- [The commands](../reference/cli.md): every option of `stateous serve`.
