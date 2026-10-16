# The HTTP API

This page lists every route of the control plane's HTTP API, its body and its answer, and every refusal code.

The `token` commands and the `Client` use this API. A supervisor uses the `/tasks` routes.

## For every route

- The API has no prefix. For example, the health route is `GET /health`.
- A body is a JSON object. An empty body is the same as `{}`. A body can be at most 1 MiB, and it can nest at most 64 levels.
- An answer is JSON, except a `204` answer, which has no body.
- The API has no authentication, no keys and no tenants. A caller that reaches the port can call every route.
- A time is ISO 8601, in UTC, with `Z`, for example `2026-10-07T05:43:39Z`.

In the paths, `{id}` is a token's id or a task's id. A task's id is a whole number.

## The token

Many routes answer with a token, in this form:

```json
{
  "token": "TICKET-1",
  "state": "coding",
  "status": "active",
  "visit": 1,
  "context": {"repo": "acme/api"},
  "entered_at": "2026-10-07T05:43:39Z",
  "parent": "PLAN-1"
}
```

| Field | Meaning |
|---|---|
| `token` | The token's id. |
| `state` | The token's state. `__start__` is START and `__end__` is END. A token that ended keeps its last state. |
| `status` | `active`, `paused`, `completed`, `failed` or `cancelled`. |
| `visit` | How many times the token entered this state: 1 on the first visit. |
| `context` | The token's context. |
| `entered_at` | When the token entered this state. |
| `parent` | Only on a child token: the token whose parallel state started it. |

## Tokens

### `POST /tokens`

Start a token.

| Body field | Meaning |
|---|---|
| `token` | The token's id: a letter or a digit, then up to 127 letters, digits, `.`, `_` or `-`. Required. |
| `context` | The token's context, as a JSON object. The default is `{}`. |

The answer is the token, with `201`. If the token exists, the answer is that token, unchanged, with `200`.

### `GET /tokens`

List the tokens, the most recently moved first. The answer is a list of tokens.

| Query | Meaning |
|---|---|
| `status` | Only the tokens with this status. |
| `parent` | Only the children of this token, for example `GET /tokens?parent=<id>`. |
| `limit` | The most tokens in the answer: 1 to 1000. The default is 100. |

### `GET /tokens/{id}`

One token. The answer is the token.

### `GET /tokens/{id}/history`

The token's history, in order. The answer is a list of entries:

```json
[{"seq": 6, "at": "2026-10-07T05:43:47Z", "kind": "event_accepted", "data": {"event": "done", "payload": null, "state": "coding"}}]
```

`seq` is the entry's number in the ledger. `kind` tells what happened, for example `entered`, `task_claimed` or `timer_fired`. `data` holds the facts of that kind. [The data](../inside/data.md) shows the entries of one token.

### `POST /tokens/{id}/events/{event}`

Fire an event on a token.

| Part | Meaning |
|---|---|
| Body field `payload` | The event's payload, as a JSON object. Optional. |
| Header `Idempotency-Key` | An idempotency key, from 1 to 256 characters, with no control characters. Optional. |

The answer is the token after the event. A second delivery with the same key changes nothing. Its answer is `{"duplicate": true, "token": {...}}`, with the token as it is now.

You cannot fire `timeout`, `error` or `joined`: the control plane fires them.

### `POST /tokens/{id}/pause`

Pause an active token. The answer is the token.

### `POST /tokens/{id}/resume`

Resume a paused token. The held events apply in order. The answer is the token.

### `POST /tokens/{id}/cancel`

Cancel an active or a paused token. The answer is the token.

The three steering routes take no body. A child token refuses them with `child_token`: steer its top token.

## The workflow

### `GET /workflow`

The graph file, as JSON. `stateous init supervisor` and `stateous token show --graph` use it. [The files](files.md) tells what the graph file holds.

### `GET /health`

The workflow's name and the graph's checksum:

```json
{"workflow": "sdlc", "checksum": "sha256:ce35bf07..."}
```

## Tasks

A supervisor uses these routes. A person or a program can also report the outcome of a waiting task.

### `POST /tasks/claim`

Take the oldest queued task of the states that the supervisor serves.

| Body field | Meaning |
|---|---|
| `supervisor` | The supervisor's name: 1 to 256 characters. Required. |
| `states` | The states that the supervisor serves: at most 1000 names. A state of a child workflow is `<workflow>/<state>`. Required. |
| `request` | A request id: 1 to 64 letters, digits, `.`, `_` or `-`. Optional. |

If no task is queued, the answer is `204`. Else the answer is the grant:

```json
{
  "task": 2,
  "token": "TICKET-1",
  "state": "testing",
  "visit": 1,
  "attempt": 1,
  "context": {"repo": "acme/api"},
  "memory": {},
  "entered_by": {"name": "done"}
}
```

`entered_by` is the event that moved the token into the state, with its `payload` if it has one. For the first state of a token, it is `__started__`.

The control plane keeps the last request id of each supervisor, and the task that it got. A claim with the same id gets the same task again, if the supervisor still holds it. Else the claim takes a new task. So a lost answer loses no task.

### `POST /tasks/heartbeat`

Renew the leases of the supervisor's tasks, in one call.

| Body field | Meaning |
|---|---|
| `supervisor` | The supervisor's name. Required. |
| `tasks` | The ids of the tasks that run: at most 1000. Required. |

The answer is `{"lost": [...]}`: the tasks that the supervisor no longer holds. The supervisor stops their routines.

### `POST /tasks/{id}/report`

The outcome of a task.

| Body field | Meaning |
|---|---|
| `kind` | `event`, `waiting` or `failed`. Required. |
| `event` | The event's name, when `kind` is `event`. |
| `payload` | The event's payload, as a JSON object. Optional. |
| `error` | Why the task failed, when `kind` is `failed`. |
| `context` | The routine's writes to the context, as a JSON object. Optional. |
| `schedules` | The timers that the routine set: a list of `{"event", "payload", "after"}`. `after` is a duration, for example `"10m"`. Optional. |
| `attempt` | The attempt that the report is for. Optional. |
| `supervisor` | The supervisor that claimed the task. A report from outside, for a waiting task, has none. |

The answer is the token after the outcome. An event that the state does not allow is refused with `event_not_allowed`.

### `POST /tasks/{id}/note`

One line of progress in the token's history.

| Body field | Meaning |
|---|---|
| `text` | The note: 1 to 500 characters, after the spaces at its two ends are removed. Required. |
| `attempt` | The attempt that the note is for. Optional. |

The answer is `204`.

### `PUT /tasks/{id}/memory`

Save one key of the routine's memory, at once.

| Body field | Meaning |
|---|---|
| `key` | The key: 1 to 128 characters. Required. |
| `value` | The value, as JSON. `null` deletes the key. Required. |
| `attempt` | The attempt that the write is for. Optional. |

The answer is `204`.

### The attempt

`attempt` is optional in a report, a note and a memory write. It names the attempt of the task that the request is for. The control plane refuses the request when the task is on a different attempt. The code is `attempt_over` when the task moved to a new attempt.

So a late report of an old attempt cannot land on a new attempt. The supervisor always sends the attempt of its grant. A request without `attempt` is for the current attempt.

## Refusals

A refusal has a sentence and a code:

```json
{"error": "task 7 is at attempt 2, not attempt 1", "code": "attempt_over"}
```

The sentence is for people, and it can change. The code does not change: a program reads the code, never the sentence.

| Code | Status | Meaning |
|---|---|---|
| `token_not_found` | 404 | No token has this id. |
| `task_not_found` | 404 | No task has this id. |
| `not_found` | 404 | The API has no such path. |
| `method_not_allowed` | 405 | The path does not take this method. |
| `bad_request` | 400, or the status of the problem (413 for a body larger than 1 MiB) | The request cannot be read: bad JSON, a bad header. |
| `invalid_argument` | 422 | A supervisor name, a request id, a status, a limit or an attempt is out of range. |
| `invalid_context` | 422 | A context or a context change does not agree with its model. |
| `invalid_payload` | 422 | A payload, a schedule, a note, a memory write or an idempotency key is not valid. |
| `invalid_token_id` | 422 | A token id breaks the pattern, or differs only in case from an existing one. |
| `invalid_body` | 422 | The body does not have the shape of the request. |
| `event_not_allowed` | 409 | The event cannot move the token in its state. |
| `token_not_active` | 409 | The token's status does not allow the request. |
| `child_token` | 409 | The token is a child of a parallel state; steer its top token. |
| `task_not_held` | 409 | The task is not this supervisor's, or no one works on it now. |
| `attempt_over` | 409 | The request is for an attempt of the task that is over: the task moved to a new attempt. |
| `database_busy` | 503 | The database cannot take the request now; try again. |
| `not_serving` | 503 | This control plane no longer holds the database: another one took it. Stop this server. |
| `unexpected` | 500 | The request failed for a reason that no code above names. |

Trying again after `not_serving` does not help on this server. A supervisor still tries again, because another control plane can take over at the same URL. Its log line gives this code.

## See also

- [The commands](cli.md): the `token` commands, which send these requests.
- [Steer tokens](../how-to/steer-tokens.md): the `Client`, and its errors.
- [The internal workflows](../inside/workflows.md): what the control plane does for each request.
- [Reliability](../concepts/reliability.md): idempotency keys, leases and attempts.
