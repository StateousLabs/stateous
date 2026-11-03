"""`answer`: run one call of a transport on a `ControlPlane`, and give its `Reply`.

The in-process transport uses it, and so does a developer's queue consumer: it takes
a `Call` off the queue, answers it, and puts the `Reply` back. A request's own fault
never raises: it is a reply with the code of the refusal."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.exc import OperationalError

from stateous.control import checks
from stateous.control.checks import MAX_BODY, MAX_DEPTH, MAX_TASK, Problem
from stateous.control.errors import ControlPlaneError, HoldLost
from stateous.control.plane import ControlPlane
from stateous.model.wire import Outcome, WireError
from stateous.transport import Call, Reply

log = logging.getLogger("stateous.answer")

UNEXPECTED = "the call failed unexpectedly"


def answer(plane: ControlPlane, call: Call) -> Reply:
    """The reply of `plane` to `call`. Synchronous: run it on a worker thread."""
    try:
        if plane.lost:
            raise HoldLost("another control plane took this database")
        _check_limits(call)
        return Reply(ok=True, body=_run(plane, call.name, call.args))
    except ControlPlaneError as error:
        return _refused(error.code, error)
    except Problem as error:
        return _refused(error.code, error)
    except WireError as error:
        return _refused("invalid_body", error)
    except OperationalError:
        return Reply(
            ok=False,
            code="database_busy",
            message="the database cannot take this call now; try again",
        )
    except Exception:
        log.exception("the call %s failed unexpectedly", call.name)
        return Reply(ok=False, code="unexpected", message=UNEXPECTED)


def _run(plane: ControlPlane, name: str, args: dict[str, Any]) -> Any:
    if name == "health":
        return plane.health()
    if name == "claim":
        supervisor = checks.supervisor_name(args.get("supervisor"))
        states = checks.state_names(args.get("states"), "states")
        request = checks.request_id(args.get("request"))
        checks.claim_wait(args.get("wait"))
        grant = plane.claim(supervisor, states, request=request, labels=args.get("labels"))
        return None if grant is None else grant.to_json()
    if name == "heartbeat":
        supervisor = checks.supervisor_name(args.get("supervisor"))
        return {"lost": plane.heartbeat(supervisor, checks.task_ids(args.get("tasks")))}
    task = _task(args.get("task"))
    if name == "report":
        named = args.get("supervisor")
        supervisor = None if named is None else checks.supervisor_name(named)
        plane.report(task, Outcome.from_json(args.get("outcome")), supervisor=supervisor)
    elif name == "note":
        text = checks.note_text(args.get("text"))
        plane.note(task, text, attempt=checks.attempt(args.get("attempt")))
    else:  # set_memory: `Call` admits only the six names
        key = checks.name(args.get("key"), "key", code="invalid_payload")
        if "value" not in args:
            raise WireError("memory needs a 'value'; null deletes the key")
        plane.set_memory(task, key, args["value"], attempt=checks.attempt(args.get("attempt")))
    return None


def _check_limits(call: Call) -> None:
    """The HTTP API's limits on a request body, for a call by any transport: at most
    `MAX_BODY` bytes of JSON, nested at most `MAX_DEPTH` levels."""
    if checks.depth_over(call.args, MAX_DEPTH):
        raise Problem(400, f"the call is nested more than {MAX_DEPTH} levels deep")
    if len(json.dumps(call.args).encode("utf-8")) > MAX_BODY:
        raise Problem(400, f"the call is larger than {MAX_BODY // (1024 * 1024)} MiB")


def _task(value: Any) -> int:
    """A task id; an id that no database integer holds is not there."""
    if type(value) is not int or value < 1:
        raise WireError("'task' is a task id: a whole number of 1 or more")
    if value > MAX_TASK:
        raise Problem(404, f"there is no task {value}", "task_not_found")
    return value


def _refused(code: str, error: Exception) -> Reply:
    message = str(error.args[0]) if error.args else type(error).__name__
    return Reply(ok=False, code=code, message=message)
