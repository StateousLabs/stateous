# The words inside

This page defines the words of how Stateous works inside. You do not need them to build a workflow. They help when you run Stateous, or read its history and its database. The words that you need to build are on [The model](../concepts/model.md).

### claim

A supervisor's request for a task of the states that it serves. The control plane answers with a grant: one queued task, its token, its visit, its attempt and its context. The task is then claimed.

### checksum

The hash of the whole graph file, for example `sha256:...`. Each change to the graph file changes it. A supervisor's `contract.py` is for one checksum.

### structure

The hash of the graph file without the `timeout=` and `retries=` of its states. A database serves one structure at a time. See [Change a workflow](../how-to/change-a-workflow.md).

### ledger

The history of each token. Each change adds one row, and the rows are never changed. `stateous token show` prints the ledger of a token.

### lease

The time for which a supervisor holds a claimed task. A heartbeat renews it. See [Tasks and routines](../concepts/tasks-and-routines.md).

### heartbeat

The message that a supervisor sends every 30 seconds for all the tasks that it holds. It renews their leases. Its answer names each task that the supervisor does not hold now, for example because the token left the state.

### outbox

The supervisor's local file of messages that the control plane did not answer yet: `.stateous/outbox.db`. See [Reliability](../concepts/reliability.md).

### idempotency key

A key that you give with an event from outside. A second event with the same key applies nothing, and gets the answer "duplicate". See [Reliability](../concepts/reliability.md).

### held event

An event that comes to a paused token. The control plane keeps it, and applies it on resume, in the order of arrival. The history shows `done held while paused`. See [Pause, resume and cancel](../concepts/pause-resume-cancel.md).
