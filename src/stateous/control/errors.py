"""The errors of a control plane. Each has `code`: the stable code of its refusal
(see the docs, Reference > The HTTP API). The API sends the code, and clients read
it, never the sentence."""

from typing import ClassVar


class ControlPlaneError(Exception):
    """The base of every error that the engine raises for a request. `code` is the
    stable code of the refusal (see the docs, Reference > The HTTP API): the API sends it,
    and clients read it, never the sentence."""

    code: ClassVar[str] = "unexpected"


class TokenNotFound(ControlPlaneError, LookupError):
    """No token has this id."""

    code: ClassVar[str] = "token_not_found"


class TaskNotFound(ControlPlaneError, LookupError):
    """No task has this id."""

    code: ClassVar[str] = "task_not_found"


class InvalidContext(ControlPlaneError, ValueError):
    """A context, or a routine's changes to it, breaks the workflow's context schema;
    or no transition from START holds on the starting context."""

    code: ClassVar[str] = "invalid_context"


class InvalidPayload(ControlPlaneError, ValueError):
    """A payload breaks its event's schema; or a schedule, a note, a memory key or
    value, or an idempotency key is not valid."""

    code: ClassVar[str] = "invalid_payload"


class InvalidArgument(ControlPlaneError, ValueError):
    """A supervisor name, a claim's request id or a listing limit is out of its range;
    or a report's `attempt` argument and its outcome's `attempt` differ."""

    code: ClassVar[str] = "invalid_argument"


class InvalidTokenId(ControlPlaneError, ValueError):
    """A token id does not match the allowed pattern, or differs only in case from
    an existing one."""

    code: ClassVar[str] = "invalid_token_id"


class EventNotAllowed(ControlPlaneError, ValueError):
    """The event cannot move the token: the workflow does not declare it, it is
    Stateous's own, the state has no transition on it, or no guard holds."""

    code: ClassVar[str] = "event_not_allowed"


class TokenNotActive(ControlPlaneError, ValueError):
    """The token is in a status that does not allow the request."""

    code: ClassVar[str] = "token_not_active"


class TaskNotHeld(ControlPlaneError, ValueError):
    """The task is not this supervisor's, or no one works on it now (or on the
    attempt that the request names)."""

    code: ClassVar[str] = "task_not_held"


class AttemptOver(TaskNotHeld):
    """The request is for an attempt of the task that is over: the task moved to a
    new attempt. It is a `TaskNotHeld`, so `except TaskNotHeld` also catches it."""

    code: ClassVar[str] = "attempt_over"


class ChildToken(ControlPlaneError, ValueError):
    """The token is a child of a parallel state: it is part of its parent's work. It
    cannot be paused, resumed or cancelled on its own; steer its top token."""

    code: ClassVar[str] = "child_token"


class GraphConflict(ControlPlaneError, RuntimeError):
    """The database has a different structure of the workflow on record, and tokens of
    it are active or paused. Serve the graph that they started on until they end, or
    cancel them."""

    code: ClassVar[str] = "graph_conflict"


class HoldLost(ControlPlaneError, RuntimeError):
    """Another control plane took this database. This one does no more work: stop it."""

    code: ClassVar[str] = "not_serving"


class NoticesOff(ControlPlaneError, RuntimeError):
    """The database has notices off. Turn them on with `stateous migrate --notices`."""

    code: ClassVar[str] = "notices_off"


class Forbidden(ControlPlaneError, PermissionError):
    """The authorizer of the control plane refused the caller this action."""

    code: ClassVar[str] = "forbidden"
