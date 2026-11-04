# Limits

This page tells what Stateous does not do, and the limits that it has today.

## What Stateous does not do

- It does not authenticate callers. `stateous serve` has no authentication: put it behind a VPN or a proxy. In your own service, your gateway checks the caller, and your authorizer decides each action. See [Tenancy](../concepts/tenancy.md).
- It serves one workflow structure for each namespace. It has no versions side by side. See [Change a workflow](../how-to/change-a-workflow.md).
- It does not launch agents. A routine launches them with PHD. See [Agents, with PHD](../concepts/agents.md).

## Known limits

- On Codex, a payload model with a `dict[str, Any]` field cannot run. See [Free-form payloads](../concepts/agents.md#free-form-payloads).
- The supervisor cannot stop a `def` routine. It ignores the result of that routine. Use `async def` for work that must stop when the token moves on.
- A paused token never ends on its own. A lease continues while the token is paused.
- `count('event')` counts the event over the whole life of the token, not for one visit. See [Guards](../concepts/guards.md).
- In a context or payload model, a field with `default_factory` can be required in `contract.py`. This occurs when the field's type is a model or a union.
- `stateous serve` has no option for a scope. For token ids in each scope, embed the control plane. See [Tenancy](../concepts/tenancy.md).
- A start key never expires. The same key in the same scope always gives the same token.
- A made id (a ULID) sorts in the order of creation only for the ids that one process makes. Ids that two replicas make in the same millisecond can sort in either order.
- `compile` cannot prove that two guards with `branches.all` or `branches.any` never overlap. Use one such guard and `otherwise=True`. See [Parallel and nested work](../concepts/parallel.md).

## Sizes

The HTTP API refuses a request that is larger than these sizes.

| What | The most |
|---|---|
| A request body | 1 MiB |
| The nesting of a body's JSON | 64 levels |
| A token id | 128 characters |
| An idempotency key, and a start key | 256 characters |
| The labels of a token | 16; a key has at most 64 characters, a value 1 to 256 |
| The label keys of a scope | 4 |
| A supervisor's name | 256 characters in a claim; 128 characters in `stateous.yaml` |
| A note | 500 characters |
| A memory key | 128 characters |
| The states in one claim, and the tasks in one heartbeat | 1000 |
| The tokens in one listing | 1000 |
| The routines that one supervisor runs at once | 1000 |

## See also

- [The HTTP API](http.md): the refusal codes.
- [Reliability](../concepts/reliability.md): what is guaranteed, and what is not.
