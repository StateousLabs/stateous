# Write a transport

This guide tells you how to connect a supervisor to a control plane over your own wire, for example a message queue. It also tells you how to prove that the transport is correct.

## Before you start

- You know the three layers: protocol, transport and policy. See [Transports](../concepts/transports.md).
- Your control plane runs in your own service. See [Embed a control plane](embed-a-control-plane.md).
- You have a way to send a message and get its reply, for example a request queue and a reply queue.

## 1. Write the supervisor's side

`CallingTransport` makes a whole transport from one function. The function sends a `Call` and gives back its `Reply`. Both have `to_json()` and `from_json()`:

```python
import json
from collections.abc import Awaitable, Callable

from stateous.transport import Call, CallingTransport, NotReached, Reply

Request = Callable[[str], Awaitable[str | None]]
"""Your queue: send one message, and give its reply, or None after a time limit."""


def over_queue(request: Request) -> CallingTransport:
    """A supervisor's transport over your queue."""

    async def send(call: Call) -> Reply:
        answer = await request(json.dumps(call.to_json()))
        if answer is None:
            raise NotReached("no reply on the jobs queue")
        return Reply.from_json(json.loads(answer))

    return CallingTransport(send, where="the jobs queue")
```

Raise `NotReached` when no reply comes. The supervisor then sends the call again, with the same request id.

## 2. Write the control plane's side

A consumer takes each call, runs it with `answer`, and sends back the reply. `answer` never raises for a bad call: it gives a reply with a refusal code.

```python
import asyncio
import json

from stateous import ControlPlane
from stateous.control.answer import answer
from stateous.transport import Call


async def reply_to(plane: ControlPlane, message: str) -> str:
    """The reply to one message from the queue."""
    call = Call.from_json(json.loads(message))
    reply = await asyncio.to_thread(answer, plane, call)
    return json.dumps(reply.to_json())
```

`answer` does not wait for work. To wake supervisors, use `plane.on_task_ready(callback)`: it tells you each state that has a task ready.

## 3. Prove it with the test kit

`check_transport` runs eight checks on your transport. It makes a small control plane for each check, and calls your function to connect to it:

```python
import asyncio

from stateous import ControlPlane
from stateous.testing import check_transport
from stateous.transport import Transport


async def connect(plane: ControlPlane) -> Transport:
    """Start a consumer for `plane`, and give the supervisor's transport."""
    raise NotImplementedError("start your consumer, and return over_queue(...)")


def test_my_queue_transport() -> None:
    asyncio.run(check_transport(connect))
```

A failure names each check that failed, for example `same_request_same_grant` when the transport drops the request id.

## 4. Run a supervisor over it

`stateous.supervise` runs the supervisor of a folder over any transport:

```python
import asyncio

from stateous import supervise
from stateous.transport import Transport


async def run(transport: Transport, stop: asyncio.Event) -> int:
    """Run the supervisor of `plan-supervisor` until `stop` is set."""
    return await supervise("plan-supervisor", transport, stop=stop, idle=25)
```

`idle` is how long a claim may wait for a task, at most 30 seconds. A transport that cannot wait answers at once; the supervisor then rests for the rest of `idle`.

A stop does not cut a claim in flight, because the control plane can grant a task in it. So a stop can take up to `idle` seconds. Behind a gateway, keep `idle` below the gateway's time limit, for example 25 seconds for a limit of 29.

## See also

- [Transports](../concepts/transports.md): the layers, and the rules that a transport keeps.
- [Embed a control plane](embed-a-control-plane.md): `ControlPlane` in your service.
- [Run supervisors](run-supervisors.md): the HTTP transport, gateways and headers.
- [The Python API](../reference/python.md): every name of `stateous.transport`.
