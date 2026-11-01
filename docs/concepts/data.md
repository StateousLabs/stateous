# Where data goes

## In one sentence

A routine reads and writes only the context: an event says why a token moved, and `into=` keeps its payload in the context.

## Why it exists

A token carries data from state to state. Without one rule, data can go to many places, and you must choose each time.

Stateous has two rules, and the API follows them:

1. **Data.** A routine reads and writes only the context. An event's name and payload say *why* the token moved. Guards and the history read the payload. If the token must keep a payload, the event declares `into=`.
2. **Decisions.** Routines decide, and guards only route. A routine returns the event that names its decision. A guard only chooses a transition, from data that is already there.

## How it works

| Place | What it is for | Who reads it | When it is saved |
|---|---|---|---|
| The context, `ctx["key"]` | The data of the token: all that later states need. | Each routine of the token, and the guards. | With the event, or not at all. |
| An event's payload | Why the token moved, for example a `Verdict`. | The guards, and the history. Not the routines. | With the event, in the history. |
| `into="key"` on an event | Keeps the event's payload in the context, as `ctx["key"]`. | Each later routine, through the context. | With the event, before the guards run. |
| `ctx.memory` | The routine's own bookkeeping, for example an agent's session id. | The routine of the same state, on a retry or a later visit. | At once. |
| `ctx.note(text)` | One line of progress, for people. | People, in the history. | At once. |
| `inputs=` on a parallel state | Data that goes down, from the parent's context to each child's context. | The routines of the child. | When the child starts. |
| `into="key"` on a parallel state | Data that comes back up: the `Branches` of the join, with each child's context. | Each later routine of the parent. | When the join is met. |

Some facts:

- A routine knows which event brought the token in: `ctx.entered_by.name`, for example `timeout`. It does not get that event's payload. For the payload, declare `into=`.
- `into=` works for each source of the event: a routine's report, `stateous token fire`, a timer and a resume.
- If a routine writes the same key that the event's `into=` names, the payload wins.
- `stateous compile` checks `into=`. The field must be in the context model, and its type must take the payload model, for example `Verdict` or `Verdict | None`.
- `stateous token show` prints the context, so you can see what each routine wrote.

## Example

This is a `triage` workflow for support tickets. `classify` decides if a ticket is urgent. `escalate` pages an engineer. `answer` writes the reply.

```python
# triage/schemas.py
from pydantic import BaseModel


class Ticket(BaseModel):
    title: str
    body: str
    on_call: str = ""  # escalate writes it
    reply: str = ""  # answer writes it
```

The rule "the body has the word 'down'" is a decision. So a routine makes it, and returns `urgent` or `normal`. A guard does not search the text.

```python
# triage-supervisor/classify.py
import re

from stateous import on_state

from contract import TicketContext, events, states


@on_state(states.classify)
def classify(ctx: TicketContext) -> events.urgent | events.normal:
    if re.search(r"\bdown\b", ctx["body"].lower()):
        return events.urgent()
    return events.normal()
```

`escalate` writes the engineer's name to the context. `answer`, a later state, reads it there:

```python
# triage-supervisor/escalate.py
from stateous import on_state

from contract import TicketContext, events, states


@on_state(states.escalate)
def escalate(ctx: TicketContext) -> events.paged:
    ctx["on_call"] = "Ada"
    ctx.note("paged Ada")
    return events.paged()
```

```python
# triage-supervisor/answer.py
from stateous import on_state

from contract import TicketContext, events, states


@on_state(states.answer)
def answer(ctx: TicketContext) -> events.replied:
    if ctx["on_call"]:
        ctx["reply"] = f"This is urgent. {ctx['on_call']} works on it now."
    else:
        ctx["reply"] = "Thank you. We reply soon."
    return events.replied()
```

`stateous token show TICKET-1` then prints the context with `on_call` and `reply`.

## Guarantees

- A routine's context changes and an event's `into=` are saved in one transaction with the event, or not at all.
- A routine never sees a payload. So it cannot depend on data that the token does not keep.
- `stateous compile` refuses an `into=` whose field is not in the context model. It also refuses a field whose type does not take the payload.

## See also

- [Context, notes and memory](context-notes-memory.md): each field of `ctx`.
- [Guards](guards.md): what a guard can read.
- [Parallel and nested work](parallel.md): `inputs=` and `into=` on a parallel state.
- [The model](model.md): the words context, payload and event.
