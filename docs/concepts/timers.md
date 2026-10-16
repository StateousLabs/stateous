# Timers and timeouts

## In one sentence

A timer fires an event on a token later, if the token is still in the same state; a state's timeout is a timer too.

## Why it exists

Some work must not wait for ever. An agent can hang, and a person can forget a review. Some work must also come back later, for example to check a build again in 10 minutes.

A routine cannot wait for hours, because a supervisor can stop. So the control plane keeps the timers in its database, and its clock fires them.

## How it works

There are two kinds of timer. They work in the same way.

| Kind | Who sets it | When | Its event |
|---|---|---|---|
| timeout | The state, with `timeout="6h"` | When the token enters the state | `timeout`, an event of Stateous |
| timer | A routine, with `ctx.schedule(event, after="10m")` | When the routine's outcome is saved | An event of your workflow |

A duration is a whole number and one unit: `30s`, `10m`, `6h` or `2d`. The longest is `3650d`.

The clock of the control plane looks for due timers every second. A due timer fires its event on the token, as if the event came from outside. The transitions and the guards of the state then choose where the token goes.

A timer is tied to one visit of the token to one state:

- When the token leaves the state, its timers stop. A timer of an earlier visit never fires.
- A retry stops the timers that the failed attempt set, because the routine runs again. The state's timeout continues: it covers all attempts.
- A paused token's timers stop. On resume, they get the paused time back. See [Pause, resume and cancel](pause-resume-cancel.md).

When no transition takes a timer's event, two different things occur:

- For a routine's timer, the control plane records the refusal, and the token stays.
- For a timeout, the token fails. A token must not stay for ever in a state that timed out.

### Set a timer in a routine

`ctx.schedule(event, after=...)` sets a timer. For an event without a payload, give the event, for example `events.done`. For an event with a payload, give the event with its payload, for example `events.failed(result)`.

The timer is saved with the routine's outcome. If the routine raises an exception, its timers are not set. A routine that returns `None` also sets its timers: they are saved at once.

You cannot set a timer for `timeout`, `error` or `joined`.

## Example

In `sdlc`, the state `coding` has a timeout of 6 hours:

```python
coding = flow.state("coding", timeout="6h", retries=2)
```

`sdlc` has no transition on `timeout`. So after 6 hours in `coding`, the token fails. To end the token in a different way, add a transition on `timeout`:

```python
flow.transition(coding, END, on="timeout")
```

A routine can give the work to a person, and set a reminder. Here, the routine of `coding` returns `None`, so the task waits. If no one reports `done` in 2 hours, the timer fires `stuck`, and `stuck` takes the token to the end:

```python
# sdlc-supervisor/coding.py: a person does the work
from stateous import on_state

from contract import TicketContext, events, states


@on_state(states.coding)
def code(ctx: TicketContext) -> None:
    ctx.note(f"waiting for a person to change {ctx['repo']}")
    ctx.schedule(events.stuck, after="2h")
    return None
```

The reminder must be shorter than the timeout. The two timers are in the same visit, and the first that is due fires first. When the token leaves `coding`, the other timer stops. So a reminder of 2 days never fires, because the timeout fails the token after 6 hours.

When the person reports `done` first, the token leaves `coding`, and the two timers stop.

The history shows each timer:

```text
timer set: timeout at 2026-10-07 05:43:39
timer set: stuck at 2026-10-07 01:43:39
timer fired: stuck
```

## Guarantees

- A timer is kept in the database, in the same transaction as the outcome that sets it. A restart of the control plane does not lose it.
- A timer fires only if the token is active and in the same visit to the same state.
- A timeout covers all attempts of a task. Retries do not give a state more time.
- Not guaranteed: the exact second. A timer fires on the first tick of the clock after it is due. If the control plane is stopped, it fires after the next start.

## See also

- [The model](model.md): the words [timer](model.md#timer) and [event](model.md#event).
- [Tasks and routines](tasks-and-routines.md): attempts and retries.
- [Guards](guards.md): guards on the event of a timer.
- [Write a routine](../how-to/write-a-routine.md): a routine that sets a timer.
