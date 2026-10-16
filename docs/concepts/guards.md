# Guards

## In one sentence

A guard is a small condition in `when=` that chooses which transition an event takes, from the data of the token and the event.

## Why it exists

One outcome can lead to different places. For example, a failed test run goes back to `coding` two times, but the third failure ends the token. The routine must not know this rule, because it does not see the graph.

So the graph holds the rule, as a guard on the transition. Stateous checks the guards when you compile, so a token never has two ways to go.

## How it works

A guard is an expression in a small language. It is not Python, and nothing runs it as Python. It can use these items:

| Item | What it reads |
|---|---|
| `ctx['key']` | A field of the token's context, with the changes of the routine. |
| `payload.field` | A field of the payload of the event. |
| `count('event')` | How many times the token accepted this event, this one included. |
| `<`, `<=`, `>`, `>=`, `==`, `!=`, `in`, `not in` | Comparisons. |
| `and`, `or`, `not` | Logic. |
| `1`, `-2.5`, `'text'`, `true`, `false`, `null`, `[1, 2]` | Numbers, strings, the three names, and lists of these. |

Some rules of the language:

- A guard reads the context as `ctx['key']`, never as `ctx.key`. It cannot read `ctx.memory`.
- On the right of `in` and `not in`, use a list, or a value from `ctx`, `payload` or `count`. A string is not permitted there.
- A value that is not set reads as `null`. A comparison of `null` with a number is false, not an error.
- `count('event')` counts the event over the whole life of the token, not for one visit.
- A guard on a transition from `START` reads only the context. A new token has no event yet, so it has no payload and no count.
- A guard on `joined` can also use `branches.all(...)` and `branches.any(...)`. See [Parallel and nested work](parallel.md).

When an event comes, the control plane looks at the transitions from the token's state on that event:

1. If one guard matches, the token takes that transition.
2. If no guard matches, the token takes the transition with `otherwise=True`.
3. If no guard matches and there is no `otherwise=True`, the control plane refuses the event. The token stays where it is.

### The overlap check

`stateous compile` makes sure that the guards on one state and one event never overlap. Two guards overlap when some data can make both of them true.

If two guards can overlap, compile stops with an error, and it gives an example of such data. Stateous never chooses between two matching guards at run time.

Compile also stops when it cannot prove that two guards never overlap. This occurs when a guard compares two values with each other, or uses `branches`. Then make one transition the fallback with `otherwise=True`.

Next to transitions with guards, a transition without a guard must have `otherwise=True`. Compile stops if it does not.

## Example

In `sdlc`, the event `failed` has two transitions from `testing`:

```python
flow.transition(testing, coding, on=failed, when="count('failed') < 3")
flow.transition(testing, END, on=failed, otherwise=True)
```

The first and second `failed` have a count of 1 and 2, so they go back to `coding`. The third has a count of 3, so the token ends.

A guard can also read the payload and the context. The `orders` starter ends a token when the review approves it:

```python
flow.transition(review, END, on=reviewed, when="payload.approved == true")
flow.transition(review, draft, on=reviewed, otherwise=True)
```

Now give the context of `orders` an `int` field `amount`. These two guards overlap:

```python
flow.transition(review, END, on=reviewed, when="ctx['amount'] > 100")
flow.transition(review, draft, on=reviewed, when="ctx['amount'] > 50")
```

`stateous compile` refuses them, and it gives an amount that makes both of them true:

```text
Error: workflow 'orders' is not valid:
  - 'reviewed' from 'review': the guards "ctx['amount'] > 100" and "ctx['amount'] > 50" can both be true, e.g. when ctx['amount'] = 101; make them exclusive, or make one the fallback (otherwise=True)
```

Change the second guard to `ctx['amount'] <= 100`, or make it the fallback.

## Guarantees

- An event takes at most one transition. Compile proves that the guards on one state and event never overlap.
- A guard has no side effects. It reads data, and it changes nothing.
- A guard reads only fields that its models have. Compile checks each `ctx['key']` against the context model, and each `payload.field` against the payload model.
- `count('event')` must name an event of the workflow, or `timeout`, `error` or `joined`.
- Not guaranteed: that an event finds a transition. With no `otherwise=True`, an event that no guard matches is refused.

## See also

- [The model](model.md): the words [transition](model.md#transition) and [guard](model.md#guard).
- [Timers and timeouts](timers.md): guards on the events `timeout` and `error`.
