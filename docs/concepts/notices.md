# Notices

## In one sentence

A notice tells your code that a token changed: an event was accepted, a token ended, or a task failed for the last time.

## Why it exists

Workflows know nothing about each other. Your code decides that the end of workflow A starts workflow B. Your code also updates a UI, or sends a message to a queue.

So your code must hear what each workflow does. Polling `stateous token show` is slow, and it can miss a change. A notice is kept in the database until your handler has it.

## How it works

Notices are a setting of the database. Turn them on with `stateous migrate --notices`. Each control plane reads the setting when it opens.

When notices are on, each of these changes writes a notice in the same transaction as the change:

| Kind | When |
|---|---|
| `event` | An event was accepted. A held event counts when a resume applies it. A rejected event gives no notice. |
| `completed`, `failed`, `cancelled` | A token ended so. A child token of a parallel state gives one too, with its `parent`. |
| `task_failed` | A task failed for the last time. A failure that will retry gives no notice. |

`ControlPlane.deliver_notices(handler)` gives the waiting notices to your handler. It deletes each notice after the handler returns. `run_notices(handler)` does this in your event loop until `close()`.

Some facts:

- **At least once.** If the process stops before the delete is saved, the handler gets the notice again.
- **In order for each token family.** A family is a top token and its child tokens. Between families there is no order.
- **The id is stable.** `notice.id` is the same at each delivery. Use it as an idempotency key.
- **A failing handler.** If the handler raises, its family's notices come again on the next call. The other families go on.
- **Keep the handler short.** It runs inside a database transaction. Hand slow work to your own queue.
- **On SQLite**, the handler must not call the same control plane. It can call another control plane.

## Example

When a token of `plan` ends, start a token of `tasks` for the same project:

```python
from stateous import ControlPlane, Notice


def connect(plan: ControlPlane, tasks: ControlPlane) -> None:
    """Start `tasks` for each `plan` token that completes."""

    def handler(notice: Notice) -> None:
        if notice.kind == "completed" and notice.parent is None:
            tasks.start(f"tasks-of-{notice.token}", {"plan": notice.token})

    plan.deliver_notices(handler)
```

To start a token that exists returns it, unchanged. So a notice that comes twice starts `tasks` once.

## Guarantees

- A change and its notice are saved in one transaction, or neither is.
- A notice is deleted only after the handler returned with no error.
- Two deliverers on one database never get the same family at the same time.

## See also

- [Embed a control plane](../how-to/embed-a-control-plane.md): a control plane in your own service.
- [Reliability](reliability.md): the ledger, and the other mechanisms.
- [The commands](../reference/cli.md): `stateous migrate --notices`.
- [The Python API](../reference/python.md): `Notice` and `ControlPlane.deliver_notices`.
