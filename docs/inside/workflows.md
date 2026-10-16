# The internal workflows

This page describes each internal workflow of Stateous as a card with five elements. Each card comes from the code of Stateous, not from a plan.

| Element | Meaning |
|---|---|
| **Trigger** | What starts the workflow: actor-initiated (a person or a program acts), event-initiated (a different workflow emitted an event), or scheduled (the clock). |
| **Steps** | The steps, in order. |
| **Actors** | Human, AI and system actors. |
| **Events** | What the workflow emits on success and on failure. Ledger entry kinds are in `code`, for example `task_claimed`. |
| **Data** | The fields that go in, and the fields that come out. |

A ledger entry is one row of the table `ledger`, with a `kind` and a `data` object. An HTTP answer and a log line are events too. A failure always emits something: a refusal with a code, a ledger entry, or a log line.

[The event graph](event-graph.md) shows how the events of one workflow start the next. [The data](data.md) shows the tables that the workflows change.

## Compile

**Trigger**

Actor-initiated. A person runs `stateous compile FLOW`. `stateous init workflow` and `stateous init supervisor` with a `flow.py` run the same steps.

**Steps**

1. Import `flow.py`, with its folder on the import path, so that it can import `schemas.py`. Then forget the imported modules.
2. Find the one workflow that no parallel state runs.
3. Check the workflow: each state can be reached, guards do not overlap, `inputs` give the child's required fields, and more. Collect all problems.
4. Build the graph document: states, events, transitions, child workflows, and each model as JSON Schema.
5. Make the text of `contract.py` in memory, to find a model name that cannot be a Python name.
6. Write the graph file. `compile` writes `build/<workflow>.json` next to `flow.py`, or the file of `--out`.

**Actors**

- Human: the developer of the workflow.
- AI: none.
- System: the `stateous` command.

**Events**

| On | Emitted |
|---|---|
| success | stdout: `wrote sdlc/build/sdlc.json`. Exit code 0. The graph file. |
| a mistake in the workflow | stderr: `error: workflow 'orders' is not valid:`, then one line for each problem. Exit code 1. No graph file. |
| `flow.py` does not import | stderr: `error: <flow.py> does not import: <error>`. Exit code 1. |
| the file cannot be written | stderr: `error: cannot write <path>: <reason>`. Exit code 1. |

**Data**

| | Fields |
|---|---|
| In | `flow.py` (a `Workflow`: name, context model, states, events, transitions) and `schemas.py` (the models). |
| Out | The graph document: `stateous` (the file format, 1), `workflow`, `context`, `states` (each with `emits`, or `parallel`, `join`, `inputs`; and `timeout`, `retries`), `events` (each with `payload`), `transitions` (`from`, `to`, `on`, `when`, `otherwise`), `workflows` (the child workflows), `schemas`. |

## Serve

**Trigger**

Actor-initiated. A person runs `stateous serve GRAPH`.

**Steps**

1. Read `--listen` as a host and a port.
2. Read the graph file, and check it.
3. Bind the port, before the database opens.
4. Open the database. On Postgres, take the serving lock first. Make or upgrade the tables.
5. Take the database for this control plane: write `serving` and `since` in the row of `control_plane`.
6. Compare the structure of the graph file with the structure on record. Refuse a new structure while tokens are active or paused. Else put the graph on record.
7. Print a warning when `--listen` lets other computers in.
8. Start the HTTP API and the clock. About each second, the clock renews the hold on the database, then ends the leases that are over, and fires the timers that are due.
9. On Ctrl-C or SIGTERM, finish the requests in progress, give up the hold, and stop.

**Actors**

- Human: the operator.
- AI: none.
- System: the `stateous` command, the control plane (API, engine, clock), the database.

**Events**

| On | Emitted |
|---|---|
| success | stderr: `serving on http://127.0.0.1:8080 (Ctrl-C stops)`. The row of `control_plane` gets `structure`, `checksum`, `serving` and `since`. |
| each tick of the clock | The steps of [Lease expiry](#lease-expiry) and [Timer fires](#timer-fires). A tick that fails logs `the clock's tick failed (...); it tries again in 1.0 seconds`. |
| a refusal at start | stderr: `error: <sentence>`. Exit code 1. For example: the port is in use, another control plane serves the database, a newer Stateous made the database, or a new structure while tokens are live. |
| another control plane takes the database | log: `another control plane took this database; ...`. Each request gets 503 with the code `not_serving`. Then stderr: `error: another control plane took the database; this control plane stopped`. Exit code 1. |
| a graceful stop | stderr: `stopped`. Exit code 0. |

**Data**

| | Fields |
|---|---|
| In | The graph file, `--db` (default `sqlite:///stateous.db`), `--listen` (default `127.0.0.1:8080`), `--supervise`. |
| Out | `control_plane`: `structure`, `checksum` (each `sha256:...`), `serving` (`host:pid:id`), `since`. `stateous_version`: the schema version. `GET /health`: `workflow`, `checksum`. |

## Start a token

**Trigger**

Actor-initiated. A person runs `stateous token start`, or a program calls `Client.start` or `POST /tokens`. A [parallel state](#join-of-a-parallel-state) starts its child tokens with the same steps.

**Steps**

1. Check the token id. If a token with this id exists, answer it, and change nothing.
2. Refuse an id that differs only in case from an existing id.
3. Add the defaults of the context model, and check the context against the model.
4. Choose the transition from `START`: the guards read only the context.
5. If the target is a parallel state, check that each child can start.
6. Write the token at `START`, and record `started` and the event `__started__`.
7. Enter the first state: record `entered`, queue a task, and set the state's timeout. A parallel state starts its children in place of a task.
8. Answer the token.

All steps are one transaction.

**Actors**

- Human: a person who starts a token.
- AI: none.
- System: the client or the command, the control plane, the database.

**Events**

| On | Emitted |
|---|---|
| success | Ledger: `started`, `event_accepted` (`__started__`), `entered`, `task_queued`, and `timer_set` for a timeout. A parallel state: `branch_started` for each child. HTTP 201 with the token. |
| the token exists | HTTP 200 with the token as it is. No ledger entry. |
| a bad id | HTTP 422, `invalid_token_id`. No ledger entry. |
| a bad context, or no transition from `START` holds | HTTP 422, `invalid_context`. No ledger entry. |

**Data**

| | Fields |
|---|---|
| In | `token` (the id), `context` (a JSON object). |
| Out | A row of `tokens`: `token`, `state`, `status` (`active`), `visit` (1), `context`, `entered_at`, `parent`, `workflow`. A row of `tasks` (`queued`). A row of `timers` for a timeout. The answer: `token`, `state`, `status`, `visit`, `context`, `entered_at`. |

## Fire an event

**Trigger**

Actor-initiated. A person runs `stateous token fire`, or a program calls `Client.fire` or `POST /tokens/{id}/events/{event}`. A webhook is the usual caller.

**Steps**

1. Check the idempotency key and the payload as JSON.
2. If a ledger entry of the token has this key, answer "duplicate", and change nothing.
3. Refuse the event when the token ended, when the event is Stateous's own, or when the workflow does not declare it.
4. Check the payload against the event's model.
5. If the token is paused, hold the event. Else choose the transition: the guards read the context, the payload and the counts of events.
6. Move the token: leave the state, then enter the target, or complete the token at `END`.

All steps are one transaction. A refusal is recorded, and then the answer is the refusal.

**Actors**

- Human: a person, or a system outside, such as CI or a webhook.
- AI: none.
- System: the client or the command, the control plane, the database.

**Events**

| On | Emitted |
|---|---|
| success | Ledger: `event_accepted` (with the key), then `task_done` or `task_cancelled`, then `entered` and `task_queued`, or `completed`. HTTP 200 with the token. |
| a paused token | Ledger: `event_deferred`. HTTP 200 with the token. |
| a duplicate key | HTTP 200 with `{"duplicate": true, "token": ...}`. No ledger entry. |
| an ended token | Ledger: `event_rejected`. HTTP 409, `token_not_active`. |
| an event that cannot move the token | Ledger: `event_rejected` with the reason. HTTP 409, `event_not_allowed`. |
| a bad payload or key | HTTP 422, `invalid_payload`. No ledger entry. |
| two guards match | Ledger: `failed` with the reason `... matched more than one guard ...`. HTTP 200 with the failed token. |
| no such token | HTTP 404, `token_not_found`. No ledger entry. |

**Data**

| | Fields |
|---|---|
| In | `token`, `event`, `payload` (optional), the header `Idempotency-Key` (optional, 1 to 256 characters). |
| Out | Ledger `event_accepted`: `event`, `payload`, `state`, and the key in the column `key`. The token's new `state`, `visit` and `status`. |

## Claim a task

**Trigger**

Event-initiated and scheduled. The claim loop of a supervisor starts after [Supervisor start](#supervisor-start) sent its outbox. It claims again at once after a task, and 1 second after an empty answer. It waits while `tasks` routines run.

**Steps**

1. The supervisor makes a request id, and sends `POST /tasks/claim` with its name, its states and the id.
2. If the id repeats the supervisor's last claim, and the supervisor still holds that attempt, the control plane gives the same task again. It renews the lease.
3. Else it looks at the 20 oldest queued tasks of these states whose tokens are active. On Postgres, it skips a task whose token is locked by a different request.
4. It marks the task `claimed`, adds 1 to its attempt, and sets a lease of 2 minutes.
5. It keeps the request id and the task in the table `claims`.
6. It answers the grant: the task, with the token's context and the routine's memory.
7. The supervisor runs the routine of the state. After a lost answer, it sends the claim again with the same id.

**Actors**

- Human: none.
- AI: none.
- System: the supervisor, the control plane, the database.

**Events**

| On | Emitted |
|---|---|
| a task | Ledger: `task_claimed` (`task`, `supervisor`, `attempt`). HTTP 200 with the grant. Log: `task 2: testing of TICKET-1, attempt 1`. |
| no task | HTTP 204. The supervisor waits 1 second. |
| the same request again | HTTP 200 with the same grant. No new ledger entry. |
| a bad body | HTTP 422, `invalid_argument` or `invalid_body`, for example for a supervisor name of more than 256 characters. Log: `the control plane refused a claim: ...`. |
| no answer | Log: `... trying again in 1s`, then 2, 4, 8, 16 and 30 seconds. The same request id. |

**Data**

| | Fields |
|---|---|
| In | `supervisor`, `states` (a child's state is `<workflow>/<state>`), `request`. |
| Out | The grant: `task`, `token`, `state`, `visit`, `attempt`, `context`, `memory`, `entered_by` (`name`, `payload`). `tasks`: `status`, `attempt`, `claimed_by`, `lease_until`. `claims`: `supervisor`, `request`, `task_id`, `attempt`. |

## Heartbeat

**Trigger**

Scheduled. Every 30 seconds, while the supervisor has running routines.

**Steps**

1. The supervisor sends `POST /tasks/heartbeat` with its name and the ids of its running tasks.
2. The control plane moves the lease of each task that the supervisor still holds to 2 minutes from now.
3. It answers the other tasks as lost: the token left the state, the token was cancelled, or the lease ended.
4. The supervisor cancels the `async def` routine of each lost task, and drops its messages from the outbox. It ignores the result of a `def` routine.

**Actors**

- Human: none.
- AI: none.
- System: the supervisor, the control plane, the database.

**Events**

| On | Emitted |
|---|---|
| success | HTTP 200 with `{"lost": [...]}`. `tasks.lease_until` moves. No ledger entry. |
| a lost task | Log: `task 2 of TICKET-1: the control plane took it back; cancelling its routine`. |
| a refusal | Log: `the control plane refused a heartbeat: ...`. |
| no answer | Log: `...; trying again in 1s`, then 2, 4, 8, 16 and 30 seconds. Until an answer comes, the leases are not renewed. After 2 minutes, [Lease expiry](#lease-expiry) runs. |
| a different failure | Log: `a heartbeat failed`, with the error. The next heartbeat comes 30 seconds later. |

**Data**

| | Fields |
|---|---|
| In | `supervisor`, `tasks` (task ids, at most 1000). |
| Out | `lost` (task ids). `tasks.lease_until`. |

## Report an outcome

**Trigger**

Event-initiated: a routine returned or raised. Also actor-initiated: someone outside reports the event of a waiting task, with `Client.report` or `POST /tasks/{id}/report`.

**Steps**

1. While the routine runs, each note and memory write goes to the outbox, then to the control plane, in order.
2. The supervisor turns the result into an outcome: an event, `waiting` for `None`, or `failed` for an exception or a bad result.
3. It waits until the notes and memory writes are sent. Then it writes the report to the outbox, and sends it with the attempt of its grant.
4. The control plane checks that the task is still worked on, by this supervisor, on this attempt.
5. `failed`: it counts a failed attempt. It queues the task again while retries are left. Else the token gets the event `error`, or fails.
6. `waiting`: it saves the context changes and the timers, and the task waits.
7. An event: it checks the event and the payload, and chooses the transition with the changed context. A paused token holds the event.
8. It saves the context changes and the timers, and moves the token as in [Fire an event](#fire-an-event).
9. The supervisor removes the report from the outbox when the control plane accepts it or refuses it.

**Actors**

- Human: a person outside who reports a waiting task.
- AI: an agent in the routine, when the routine runs one.
- System: the routine, the supervisor, the outbox, the control plane, the database.

**Events**

| On | Emitted |
|---|---|
| an event | Ledger: `context_changed` (if any), `timer_set` (if any), `event_accepted`, `task_done`, then `entered` and `task_queued`, or `completed`. Log: `task 2 of TICKET-1: reported passed`. |
| `waiting` | Ledger: `task_waiting`. |
| `failed`, retries left | Ledger: `task_failed` (`error`), `task_retrying` (`attempt`). The task is `queued` again. |
| `failed`, no retries left | Ledger: `task_failed`, then `event_accepted` for `error`, or `failed` with the reason. |
| a note | Ledger: `note` (`task`, `text`). HTTP 204. |
| a memory write | Ledger: `memory_set` or `memory_deleted` (`state`, `memory_key`). Never the value. HTTP 204. |
| an event that the state cannot emit, or that no guard takes | Ledger: `event_rejected`. HTTP 409, `event_not_allowed`. The supervisor reports a failure: `the control plane refused the report: ...`. |
| an attempt that is over | HTTP 409, `attempt_over`. Log: `task 2 of TICKET-1: the control plane refused the report: ...`. The supervisor drops it. |
| a task that this supervisor does not hold now | HTTP 409, `task_not_held`, for example after the lease ended and the task was queued again. The supervisor sends a heartbeat to check, then drops the report. |
| no such task | HTTP 404, `task_not_found`. The supervisor drops the report. |
| a bad context change | HTTP 422, `invalid_context`. The supervisor reports a failure. |
| no answer | Log: `...; trying again in 1s`, then 2, 4, 8, 16 and 30 seconds. The report stays in the outbox. |
| no answer at a stop | Log: `task 2 of TICKET-1: the control plane cannot be reached; the report stays in the outbox for the next start`. |

**Data**

| | Fields |
|---|---|
| In | `kind` (`event`, `waiting`, `failed`), `event`, `payload`, `error`, `context` (the routine's writes), `schedules` (`event`, `payload`, `after`), `attempt`, `supervisor`. |
| Out | The token: `context`, `state`, `visit`, `status`. `tasks.status`. New rows of `timers`. The answer: the token. |

## Lease expiry

**Trigger**

Scheduled. Each tick of the clock of [Serve](#serve), about each second, looks for claimed tasks whose leases ended.

**Steps**

1. Find each claimed task with a lease that ended.
2. For each task, in its own transaction: read it again. Skip it when it is no longer claimed, or when a heartbeat renewed it.
3. End the lease, and count a failed attempt with the reason `<supervisor> stopped answering: no heartbeat for 2 minutes`.
4. Continue as a `failed` report: queue the task again while retries are left. Else the token gets `error`, or fails.

**Actors**

- Human: none.
- AI: none.
- System: the clock, the control plane, the database.

**Events**

| On | Emitted |
|---|---|
| retries left | Ledger: `task_failed` (`build-box-1 stopped answering: no heartbeat for 2 minutes`), `task_retrying`. The task is `queued`. |
| no retries left | Ledger: `task_failed`, then `event_accepted` for `error`, or `failed` with the reason `'coding' failed, and has no transition on 'error': ...`. |
| a paused token, no retries left | Ledger: `task_failed`, then `event_deferred` for `error`. |
| a late report of that attempt | HTTP 409, `attempt_over` (a new attempt started) or `task_not_held`. |

**Data**

| | Fields |
|---|---|
| In | `tasks`: `status`, `lease_until`, `claimed_by`, `attempt`. The state's `retries`. |
| Out | `tasks`: `status` (`queued` or `failed`), `claimed_by` (cleared for a retry), `lease_until` (cleared). The routine's own timers of the attempt are cancelled. |

## Timer fires

**Trigger**

Scheduled. Each tick of the clock of [Serve](#serve) looks for pending timers that are due, of active tokens.

**Steps**

1. Find each pending timer that is due, in the order of its time.
2. For each timer, in its own transaction: read it again. Skip it when the token is not active, or when the token left the state and visit of the timer.
3. Mark the timer `fired`, and record `timer_fired`.
4. Route its event as in [Fire an event](#fire-an-event).
5. When the event cannot move the token: a timeout fails the token. A routine's timer records the refusal, and the token stays.

**Actors**

- Human: none.
- AI: none.
- System: the clock, the control plane, the database.

**Events**

| On | Emitted |
|---|---|
| success | Ledger: `timer_fired` (`event`), then `event_accepted` and the move. |
| a timeout that no transition takes | Ledger: `timer_fired`, then `failed` with the reason `'coding' timed out: ...`. |
| a routine's timer that cannot move the token | Ledger: `timer_fired`, `event_rejected`. |
| the token left the state | Nothing. A move cancels the timers of the state. |

**Data**

| | Fields |
|---|---|
| In | `timers`: `token`, `state`, `visit`, `event`, `payload`, `due_at`, `kind` (`timeout` or `timer`). |
| Out | `timers.status` (`fired`). The token's move. |

## Pause and resume

**Trigger**

Actor-initiated. A person runs `stateous token pause` or `stateous token resume`, or a program calls the client or `POST /tokens/{id}/pause` and `/resume`.

**Steps**

1. Refuse a child token: steer its top token.
2. Pause: refuse a token that is not active. Set the status `paused`, also for each active child.
3. While the token is paused, no supervisor gets its queued tasks, and its timers do not fire. Each event that arrives is held, with its place in the order of arrival.
4. Resume: refuse a token that is not paused. Move each pending timer later by the paused time, and set the status `active`.
5. Apply the held events of the token and its children, in the order that they arrived. Drop a held event that no longer fits, and record why.

**Actors**

- Human: the operator.
- AI: none.
- System: the client or the command, the control plane, the database.

**Events**

| On | Emitted |
|---|---|
| pause | Ledger: `paused`. HTTP 200 with the token. |
| an event while paused | Ledger: `event_deferred` (`event`, `payload`, `arrival`). For a report: also `state`, `visit`, `task`, `context`, `schedules`. |
| resume | Ledger: `resumed`, then `event_replayed` and the move, or `event_dropped` (`event`, `reason`) for each held event. HTTP 200. |
| a token in the wrong status | HTTP 409, `token_not_active`. |
| a child token | HTTP 409, `child_token`. |
| no such token | HTTP 404, `token_not_found`. |

**Data**

| | Fields |
|---|---|
| In | The token id. |
| Out | `tokens.status`. `timers.due_at`. The held events, as ledger entries. |

## Cancel

**Trigger**

Actor-initiated. A person runs `stateous token cancel`, or a program calls `Client.cancel` or `POST /tokens/{id}/cancel`. Event-initiated for a child token: its parent left the parallel state, failed, or was cancelled.

**Steps**

1. Refuse a child token: cancel its top token.
2. Refuse a token that is not active or paused.
3. Drop each held event of a paused token.
4. Cancel the tasks of the state, its pending timers, and each live child.
5. Set the status `cancelled`, and delete the token's memory.
6. At its next heartbeat, the supervisor of a claimed task learns that the task is lost.

**Actors**

- Human: the operator.
- AI: none.
- System: the client or the command, the control plane, the database.

**Events**

| On | Emitted |
|---|---|
| success | Ledger: `event_dropped` (a paused token), `task_cancelled`, `cancelled`. HTTP 200 with the token. A child: `cancelled` with a `reason`. |
| an ended token | HTTP 409, `token_not_active`. |
| a child token | HTTP 409, `child_token`. |
| no such token | HTTP 404, `token_not_found`. |

**Data**

| | Fields |
|---|---|
| In | The token id. |
| Out | `tokens.status` (`cancelled`). `tasks.status` (`cancelled`). `timers.status` (`cancelled`). The rows of `memory` of the token are deleted. |

## Join of a parallel state

**Trigger**

Event-initiated. A child token completes or fails. Its parent is in the parallel state that started the child.

**Steps**

1. Find the parent. Stop when the parent is not active, or left the state that started the child.
2. With `join="all"`, wait until each child of the state ended. With `join="any"`, continue at once.
3. Make the payload of `joined`: one entry for each child that ended (`all`), or for this child (`any`).
4. Route `joined` on the parent, as in [Fire an event](#fire-an-event). The guards can read `branches`.
5. When the parent leaves the state, cancel each child that has not ended.
6. When no transition takes `joined`, the parent fails.

All steps are in the transaction in which the child ended.

**Actors**

- Human: none.
- AI: none.
- System: the control plane, the database.

**Events**

| On | Emitted |
|---|---|
| the join is met | Ledger of the parent: `event_accepted` for `joined`, then the move. Each other live child: `cancelled` with the reason `the parent left 'reviews' on 'joined'`. |
| the join is not met yet | Nothing. |
| no transition takes `joined` | Ledger of the parent: `failed` with the reason `the join of 'reviews' cannot move the token: ...`. |

**Data**

| | Fields |
|---|---|
| In | The child's `status` and `state`, and the payload of the event that brought it into that state. The state's `join`. |
| Out | The payload `{"branches": [...]}`: for each child, `workflow`, `token`, `status`, `final`, `payload`. |

## Supervisor start

**Trigger**

Actor-initiated. A person runs `stateous supervise FOLDER`, or `stateous serve --supervise FOLDER`.

**Steps**

1. Read `stateous.yaml`, and the checksum in `contract.py`.
2. Open the outbox, `.stateous/outbox.db`, with a lock. A second supervisor on the folder cannot open it.
3. Ask the control plane for `GET /health`. When it cannot be reached, try again after 1, 2, 4, 8, 16, then every 30 seconds.
4. Compare the checksum of the control plane with the checksum of `contract.py`.
5. Import each Python file of the folder, and find the routines.
6. Start the heartbeat loop and the claim loop.
7. Before the first claim, send the messages that are left in the outbox, in order. A message whose task has an earlier message that did not go stays.
8. Then start to [claim tasks](#claim-a-task).

**Actors**

- Human: the operator.
- AI: none.
- System: the `stateous` command, the supervisor, the outbox, the control plane.

**Events**

| On | Emitted |
|---|---|
| success | Log: `build-box-1 serves draft, review; the control plane is http://127.0.0.1:8080`. |
| the control plane cannot be reached | Log: `cannot reach the control plane at <url> (...); trying again in 1s`, then 2, 4, 8, 16 and 30 seconds. The supervisor claims no task until it gets an answer. |
| the URL is not a Stateous control plane | stderr: `error: <url> did not answer as a Stateous control plane: ...`. Exit code 1. |
| messages left | Log: `sending 2 message(s) left in the outbox`, then one line for each report. |
| a contract for a different graph | stderr: `error: ... is for another version of the workflow ...: run stateous init supervisor ... again`. Exit code 1. |
| a second supervisor on the folder | stderr: `error: another supervisor runs on <folder> now: stop it, or use another folder`. Exit code 1. |
| no routine, two routines for a state, or a file that does not import | stderr: `error: <sentence>`. Exit code 1. |
| the first stop signal | Log: `stopping: no new tasks; waiting for 1 routine(s) (signal again to stop now)`. Exit code 0 when the routines end. |
| the second stop signal | Log: `stopping now: cancelling 1 routine(s)`. Exit code 1. |

**Data**

| | Fields |
|---|---|
| In | `stateous.yaml`: `url`, `tasks`, `name`. `contract.py`: `__checksum__`, the states. `GET /health`: `workflow`, `checksum`. The outbox: `messages`. |
| Out | The served states. The messages of the outbox, sent and removed. |

## See also

- [The event graph](event-graph.md): how these workflows start each other.
- [The data](data.md): the tables, the outbox, and the rows of one token.
- [Reliability](../concepts/reliability.md): the guarantees that these workflows give.
- [The HTTP API](../reference/http.md): the routes that start these workflows.
- [The commands](../reference/cli.md): the commands that start these workflows.
