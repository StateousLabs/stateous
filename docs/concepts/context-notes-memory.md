# Context, notes and memory

## In one sentence

A routine gets `ctx`, which holds the token's context, the routine's own memory, and a way to write notes to the token's history.

## Why it exists

A routine needs three kinds of data, and each one has different rules:

- **The context** is the business data of the token, for example the path of a checkout. The guards read it, so a change to it must agree with the outcome.
- **A note** tells a person what the routine does now, for example each step of an agent.
- **The memory** is bookkeeping for the routine, for example an agent's session id. It must stay after a crash, because the next attempt needs it.

## How it works

These are the fields of `ctx`:

| Field | What it gives |
|---|---|
| `ctx["key"]` | A value of the token's context. It raises `KeyError` for a key that is not set. |
| `ctx["key"] = value` | A change to the token's context. It is saved with the outcome, or not at all. |
| `ctx.get("key", default)` | The same as `ctx["key"]`, with a default value for a key that is not set. |
| `ctx.memory` | The routine's own memory, for this token and this state. Each change is saved at once. |
| `ctx.note(text)` | One line of progress in the token's history. It is sent at once. |
| `ctx.token` | The token's id. |
| `ctx.task` | The task's id. |
| `ctx.visit` | 1 the first time the token is in this state, 2 the next time, and so on. |
| `ctx.attempt` | 1, then 2 after a retry, and so on. |
| `ctx.entered_by` | The event that brought the token here: `.name` and `.payload`. In the first state, `.name` is `__started__`. |
| `ctx.schedule(event, after="10m")` | A timer. See [Timers and timeouts](timers.md). |

### The context

A value that you read from `ctx` is a copy. So `ctx["items"].append(2)` changes nothing. Write the new value with `ctx["items"] = ...`. Values must be JSON.

The supervisor sends the changes with the report. The control plane checks them against the context model. Changes that do not agree with the model fail the task. A failed attempt saves no change to the context.

### Notes

`ctx.note(text)` adds one line to the token's history. A note has up to 500 characters. A longer note raises `ValueError`. Empty text does nothing, so you can give each event of an agent to `ctx.note`.

A note is sent at once, through the supervisor's outbox. It does not wait for the outcome.

### The memory

`ctx.memory` is a small key-value store. Stateous keeps one for each token **and** state. Use it for bookkeeping, for example an agent's session id. Do not use it for the workflow's data.

| Property | Rule |
|---|---|
| Scope | One token and one state. `TICKET-1` in `coding` and `TICKET-1` in `testing` have different memories. |
| Saving | At once, with `PUT /tasks/{id}/memory`, not with the outcome. So it stays after a crash. |
| Keys and values | A key has 1 to 128 characters. A value is JSON. |
| Delete a key | `del ctx.memory["dev"]`. A value of `None` raises `ValueError`. |
| Retries | The next attempt sees it, so it can resume a crashed agent session. |
| Later visits | The token sees it again when it comes back to the state. |
| Several agents in one state | The routine chooses the keys, for example `memory["dev"]` and `memory["review"]`. |
| A new session for each visit | Put the visit in the key: `memory[f"dev:{ctx.visit}"]`. |
| End | It is deleted when the token ends: completed, failed or cancelled. |
| History | The history records the key of each change, never the value: `memory set: dev (in coding)`. |
| Guards | They cannot read it. |

The context and the memory follow different rules:

| | The context | The memory |
|---|---|---|
| Saved | With the outcome, or not at all | At once |
| After a failed attempt | The changes are lost | The changes stay |
| Seen by | The whole token: each state, and the guards | One state of one token |
| Checked against a model | Yes | No: any JSON value |

## Example

The routine of `coding` in `sdlc` runs an agent. It keeps the agent's session id in `ctx.memory`, and it writes each event of the agent as a note:

```python
async with phd.Dispatcher(spec).run(inputs, session_id=ctx.memory.get("dev")) as handle:
    async for ev in handle.events():
        if handle.session_id and "dev" not in ctx.memory:
            # known when the harness reports it; saved at once, so a crash keeps it
            ctx.memory["dev"] = handle.session_id
        ctx.note(ev.summary)  # progress in the token's history; "" gives no note
    result = await handle.wait()
```

If the supervisor stops, the next attempt reads `ctx.memory.get("dev")`, and the agent continues its session. The whole routine is on [Agents, with PHD](agents.md).

A routine can also write the context. Here, the routine of `testing` keeps the last result for a later state:

```python
@on_state(states.testing)
def test(ctx: TicketContext) -> events.passed | events.failed:
    result = run_tests(ctx["repo"])
    ctx["last_failed"] = result.failed  # saved with the event
    return events.passed(result) if result.failed == 0 else events.failed(result)
```

For this, `Ticket` must have a field `last_failed`. Else the control plane refuses the change, and the task fails.

## Guarantees

- Changes to the context are saved with the outcome in one transaction, or not at all.
- Notes and memory writes are sent in order, before the report of the same task.
- A memory write is on disk in the supervisor's outbox before it is sent. A crash does not lose it, if the supervisor starts again before the task's lease ends.
- A note or a memory write for an attempt that is over is refused. An old attempt cannot write to a new one.
- Not guaranteed: that the memory stays after the token ends. It is deleted when the token ends.

## See also

- [The model](model.md): the words [context](model.md#context), [note](model.md#note) and [memory](model.md#memory).
- [Agents, with PHD](agents.md): the memory keeps an agent's session.
- [Reliability](reliability.md): the supervisor's outbox.
- [Write a routine](../how-to/write-a-routine.md): the routine that reads and writes `ctx`.
