# Tenancy

## In one sentence

Labels scope tokens and tasks to your tenants, and your own authorizer decides who may do what with them.

## Why it exists

Stateous does not know what a user, a tenant or a plan is. Your product knows. One product keeps every tenant in one workflow. Another product runs a control plane for each tenant.

So Stateous gives two small tools, and no tenancy model. Your code decides how to use them.

## How it works

**Labels.** A token gets labels at start, for example `{"user": "u42", "project": "p7"}`.

- A token has at most 16 labels. A key is a lower-case letter, then up to 63 lower-case letters, digits, `_`, `.` or `-`. A value is a string of 1 to 256 characters.
- Labels never change after the start. A child token of a parallel state gets its parent's labels.
- A claim with labels takes only a task whose token has each of them. A claim with no labels takes any task.
- A list of tokens can filter by labels. Notices and "task ready" carry the labels.

**The authorizer.** `ControlPlane(..., authorize=fn)` calls `fn(caller, action, labels)` for each call. It returns True to allow. A refusal raises `Forbidden`, with the code `forbidden`; over HTTP it is 403.

| Action | Labels that it gets |
|---|---|
| `start` | The labels that the start gives. |
| `claim` | The labels that the claim asks for, `{}` for none. |
| `report`, `note`, `memory`, `heartbeat` | The labels of the task's token. A refused heartbeat task counts as lost. |
| `fire`, `pause`, `resume`, `cancel`, `read` | The labels of the token. A list keeps only the tokens that the caller may read. |

**The caller.** Stateous never makes a caller. Each transport passes one through:

- HTTP: `create_app(plane, caller=fn)`, where `fn(request)` reads, for example, a user id that your gateway checked.
- In process: `InProcess(plane, caller=...)`.
- Your queue: `answer(plane, call, caller=...)`.
- Your own code: the keyword `caller` of each `ControlPlane` method.

**Entitlements.** `plane.count_claimed(labels)` counts the tasks claimed now for those labels. Your authorizer can use it, for example to allow about 3 tasks at a time on a plan. It is a soft limit: two claims at the same moment can both see 2, and both claim.

## Example

Each user may act only on the tokens of that user. The service itself calls with no caller:

```python
from collections.abc import Mapping

from stateous.control.access import Action


def only_own(caller: object, action: Action, labels: Mapping[str, str]) -> bool:
    """A user may act only on the tokens with the label `user` of that user."""
    return caller is None or labels.get("user") == caller
```

A supervisor of user `u42` then claims with `labels={"user": "u42"}`, and its requests carry `u42` as the caller.

## Guarantees

- A refused call changes nothing.
- A claim with labels never takes a task of a token without those labels.
- A waiting claim with labels is not woken by a task of other labels.

## See also

- [Run replicas](../how-to/run-replicas.md): several control planes of one workflow.
- [Transports](transports.md): how the caller reaches the control plane.
- [Embed a control plane](../how-to/embed-a-control-plane.md): `ControlPlane` in your service.
- [The Python API](../reference/python.md): `Forbidden`, `Action` and `Authorize`.
