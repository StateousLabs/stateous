# The Python API

This page lists every public name of the `stateous` package and of `stateous.client`. The text comes from the docstrings in the code.

Import each name from `stateous`, for example `from stateous import Workflow`. Import the client's errors from `stateous.client`.

## Write a workflow

You write a workflow in `flow.py`, with `Workflow`, `START` and `END`. `stateous compile` reads it. See [Write a workflow](../how-to/write-a-workflow.md).

::: stateous.Workflow

::: stateous.START

::: stateous.END

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
