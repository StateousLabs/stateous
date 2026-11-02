# The Python API

This page lists every public name of the `stateous` package and of `stateous.client`. The text comes from the docstrings in the code.

Import each name from `stateous`, for example `from stateous import Workflow`. Import the client's errors from `stateous.client`.

## Write a workflow

You write a workflow in `flow.py`, with `Workflow`, `START` and `END`. `stateous compile` reads it. See [Write a workflow](../how-to/write-a-workflow.md).

::: stateous.Workflow

::: stateous.START

::: stateous.END

A parallel state with `into=` keeps the data of its join in the context, as `Branches`. See [Parallel and nested work](../concepts/parallel.md).

::: stateous.Branches

::: stateous.Branch

## Write a routine

A routine is a function in the supervisor's folder. `@on_state` marks it, `ctx` is its `Context`, and it returns an `Occurrence`. The generated `contract.py` gives the `StateRef` of each state. See [Write a routine](../how-to/write-a-routine.md).

::: stateous.on_state

::: stateous.Context
    options:
      merge_init_into_class: false
      show_attribute_values: false

::: stateous.StateRef

::: stateous.Occurrence

::: stateous.OutcomeError

## Talk to the control plane

A program outside uses `Client` to start, fire, read and steer tokens. Each method sends one request of [the HTTP API](http.md). See [Steer tokens](../how-to/steer-tokens.md).

::: stateous.Client

::: stateous.client.ClientError

::: stateous.client.NotFound

::: stateous.client.Refused

::: stateous.client.Unreachable

## Run a control plane in your service

`ControlPlane` is one workflow over one database, in your own process. `stateous serve` uses it too.

::: stateous.ControlPlane

::: stateous.Token

::: stateous.HistoryEntry

::: stateous.Fired

A notice tells your handler of a change. See `ControlPlane.deliver_notices`.

::: stateous.Notice

::: stateous.NoticeKind

Each error of a control plane has `code`, the stable code of its refusal. The HTTP API sends the same codes.

::: stateous.ControlPlaneError

::: stateous.TokenNotFound

::: stateous.TaskNotFound

::: stateous.InvalidContext

::: stateous.InvalidPayload

::: stateous.InvalidArgument

::: stateous.InvalidTokenId

::: stateous.EventNotAllowed

::: stateous.TokenNotActive

::: stateous.TaskNotHeld

::: stateous.AttemptOver

::: stateous.ChildToken

::: stateous.GraphConflict

::: stateous.HoldLost

::: stateous.NoticesOff

These errors come from the database, when a control plane opens.

::: stateous.SchemaNotReady

::: stateous.DatabaseHeld

::: stateous.ForeignDatabase
