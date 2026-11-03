# Transports

## In one sentence

A transport carries the calls of a supervisor to a control plane: in one process, over HTTP, through a gateway, or over your own queue.

## Why it exists

Stateous must not decide how the parts of your product talk. One team runs everything on one computer. Another team puts a gateway in front of each service, or sends each message through a broker.

So Stateous keeps three things apart:

| Layer | What it is | Who owns it |
|---|---|---|
| The protocol | The six calls of a supervisor, and their rules: leases, attempts, request ids, refusal codes, the outbox. | Stateous. It does not change. |
| The transport | How one call reaches the control plane, and how its reply comes back. | You. Stateous ships two. |
| The policy | Who may claim which tasks: users, tenants, authentication. | You. |

## How it works

A supervisor makes six calls: `health`, `claim`, `report`, `heartbeat`, `note` and `set_memory`. A transport makes **one attempt** for each call. It returns the reply, or it raises one of two errors:

- `Refused`: the control plane answered no. It has a stable `code`, for example `task_not_held`. To try again does not help.
- `NotReached`: no answer came. The call may or may not have arrived.

The supervisor tries a call that is not reached again, after 1, 2, 4, 8, 16, then every 30 seconds. A claim sent again keeps its request id, so a lost reply never gives a task twice. A transport does not copy this logic.

Two codes mean "try again": `database_busy` and `not_serving`. A transport raises `NotReached` for them.

Stateous ships two transports:

- **HTTP**, the default of `stateous supervise`. It takes extra headers, for example a bearer token, and a URL with a path, for a gateway.
- **In process**, for a supervisor in the control plane's process. `serve --supervise` uses it.

For any other wire, `CallingTransport` needs one function: send a `Call`, give back its `Reply`. Both are plain JSON. On the control plane's side, `answer(plane, call)` gives the reply.

**Claims that wait.** A claim can wait up to 30 seconds at the control plane for a task. The control plane wakes it when a task is ready. So a supervisor does not have to poll. `ControlPlane.on_task_ready` tells your own code when a task is ready, with its state and its token's labels. Use it, for example, to publish to a queue for that tenant.

## Example

A transport over a queue, in a few lines. The `send` function is yours:

```python
from collections.abc import Awaitable, Callable

from stateous.transport import Call, CallingTransport, Reply


def queue_transport(send: Callable[[Call], Awaitable[Reply]]) -> CallingTransport:
    """A supervisor's transport over your own queue."""
    return CallingTransport(send, where="the jobs queue")
```

[Write a transport](../how-to/write-a-transport.md) shows both sides, and the test kit.

## Guarantees

- A transport never decides who holds a task. The control plane's database decides, with leases and attempts.
- A claim sent again with the same request id gets the same task. A lost reply loses no task.
- `stateous.testing.check_transport` checks that a transport keeps these rules. It cannot see every fault, for example a reply that a broker loses only under load.

## See also

- [Write a transport](../how-to/write-a-transport.md): a queue transport, step by step.
- [Run supervisors](../how-to/run-supervisors.md): HTTP, gateways and headers.
- [Embed a control plane](../how-to/embed-a-control-plane.md): the control plane in your own service.
- [The HTTP API](../reference/http.md): the routes, and the codes of each refusal.
