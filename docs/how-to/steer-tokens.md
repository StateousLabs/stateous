# Steer tokens

This guide tells you how to start, fire, show, pause, resume and cancel tokens, from the command line and from Python.

Each command sends one HTTP request to the control plane. See [Pause, resume and cancel](../concepts/pause-resume-cancel.md) for what each action does to a token.

## Point the commands at the control plane

Each `token` command takes `--url`. The default is the environment variable `STATEOUS_URL`, else `http://127.0.0.1:8080`.

```bash
export STATEOUS_URL=http://cp.internal:8080
```

## 1. Start a token

```bash
stateous token start TICKET-1 '{"repo": "acme/api"}'
```

The second argument is the token's context, as a JSON object. The control plane checks it against the context model. The command prints the token's line, for example `TICKET-1: coding, active`.

A token id has a letter or a digit, then up to 127 letters, digits, `.`, `_` or `-`. A new id that differs only in case from an existing id is refused. `start` with the id of a token that exists shows that token, and does not start a second one.

## 2. Fire an event

```bash
stateous token fire TICKET-1 done
stateous token fire TICKET-1 failed '{"passed": 3, "failed": 1}'
stateous token fire TICKET-1 done --key gh-delivery-81231
```

The optional argument after the event's name is the payload, as a JSON object. You cannot fire `timeout`, `error` or `joined`.

`--key` gives an idempotency key, from 1 to 256 characters. Give the id of the webhook delivery, for example. A second delivery with the same key applies nothing, and gets the answer "duplicate":

```text
$ stateous token fire TICKET-1 done --key gh-delivery-1
TICKET-1: testing, active
$ stateous token fire TICKET-1 done --key gh-delivery-1
duplicate: TICKET-1: testing, completed
```

The duplicate shows the token as it is now. A key stays used for the life of the token, also across a pause.

## 3. Show tokens

```bash
stateous token show TICKET-1
stateous token show
stateous token show --graph
```

- With an id, `show` prints the token's line, its children, and its history. The times are local.
- With no id, it lists the tokens, at most 100, the most recently moved first.
- `--graph` draws the workflow as a Mermaid diagram.

```text
TICKET-1: testing, completed
23:43:39  #1  started
23:43:39  #2  __started__ in START
23:43:39  #3  entered coding
23:43:39  #4  task 1 queued for coding
23:43:39  #5  timer set: timeout at 2026-10-07 05:43:39
23:43:47  #6  done in coding
23:43:47  #7  task 1 cancelled
23:43:47  #8  entered testing
23:43:47  #9  task 2 queued for testing
23:43:47  #10  timer set: timeout at 2026-10-07 00:43:47
23:43:48  #11  build-box-1 took task 2 (attempt 1)
23:43:51  #12  passed in testing: {"passed": 1, "failed": 0}
23:43:51  #13  task 2 done
23:43:51  #14  completed
```

Here, `done` came from outside, so no supervisor took task 1. The history shows it as `cancelled`. A token that ended keeps its last state in the first line.

## 4. Pause, resume or cancel

```bash
stateous token pause TICKET-1
stateous token resume TICKET-1
stateous token cancel TICKET-1
```

- `pause` works on an active token. Its events wait, its tasks wait, and its timers stop.
- `resume` works on a paused token. The events that waited apply in the order that they arrived.
- `cancel` works on an active or a paused token. A cancelled token takes no more events.

You cannot pause, resume or cancel a child token of a parallel state. Steer its parent. See [Parallel and nested work](../concepts/parallel.md).

## When the control plane refuses

An error prints one sentence to stderr, and the exit code is 1:

```text
$ stateous token fire TICKET-4 done
error: the control plane at http://127.0.0.1:8080 refused (409): token 'TICKET-4' is cancelled; it takes no events
```

The history also records a refused event, for example `done refused: ...`.

## From Python: the client

Use `Client` for the same actions in a program:

```python
from stateous import Client

with Client("http://cp.internal:8080") as c:
    c.start("TICKET-1", {"repo": "acme/api"})
    c.fire("TICKET-1", "done", key="gh-delivery-81231")  # key: idempotency
    token = c.get("TICKET-1")
    history = c.history("TICKET-1")
    listed = c.tokens(status="active")
    c.pause("TICKET-1")
    c.resume("TICKET-1")
    c.cancel("TICKET-1")
```

Each method returns the JSON answer of the control plane. Without `with`, call `c.close()` at the end.

The client raises these errors, from `stateous.client`:

- `Refused`, when the control plane refuses a call. It has `.status`, `.message` and `.code`. Use `.code` in your program, for example `attempt_over`. `.message` is for people, and it can change.
- `NotFound`, a `Refused` with the status 404: no such token, task or path.
- `Unreachable`, when the control plane does not answer.

## Report the outcome of a waiting task

A routine that returns `None` leaves its task waiting. The work goes on outside, for example in CI. When it ends, report the event with the task's id:

```python
from stateous import Client

with Client("http://cp.internal:8080") as c:
    c.report(7, "passed", {"passed": 12, "failed": 0}, attempt=1)
```

With `attempt=N`, the control plane refuses the report (409) when the task is not on attempt N. The task's id is `ctx.task` in the routine, and it is in the history.

## Over HTTP

Each command is one request. For example, `token fire` with `--key` is this request:

```text
POST /tokens/TICKET-1/events/done
Idempotency-Key: gh-delivery-81231

{}
```

The `Idempotency-Key` header is the key. A payload goes in the body, as `{"payload": {...}}`.

## See also

- [Pause, resume and cancel](../concepts/pause-resume-cancel.md): held events, and the order of replay.
- [Reliability](../concepts/reliability.md): idempotency keys.
- [Tasks and routines](../concepts/tasks-and-routines.md): tasks that wait.
- [The model](../concepts/model.md): the words [token](../concepts/model.md#token) and [idempotency key](../concepts/model.md#idempotency-key).
- [The commands](../reference/cli.md): every `token` command and its options.
- [The HTTP API](../reference/http.md): every route, and the refusal codes.
