"""The neutral messages of a transport: a `Call` to the control plane, and its `Reply`.

Both are plain JSON, so any wire can carry them: a queue, a socket, a file. HTTP
does not use them; it maps its routes and statuses to the same calls and codes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, cast, get_args

from stateous.model.wire import WireError

CallName = Literal["health", "claim", "report", "heartbeat", "note", "set_memory"]
"""The six calls of a supervisor."""

CALL_NAMES: frozenset[str] = frozenset(get_args(CallName))

RETRY_CODES: frozenset[str] = frozenset({"database_busy", "not_serving"})
"""Codes that mean "try again": the call is not reached, not refused."""

CONFLICT_CODES: frozenset[str] = frozenset(
    {"event_not_allowed", "token_not_active", "child_token", "task_not_held", "attempt_over"}
)
"""Codes of a request that the token's or the task's state does not allow."""


@dataclass(frozen=True)
class Call:
    """One call of a supervisor: its name, and its arguments as JSON."""

    name: CallName
    args: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "args": self.args}

    @classmethod
    def from_json(cls, data: Any) -> Call:
        if not isinstance(data, dict):
            raise WireError("a call must be a JSON object")
        body = cast("dict[str, Any]", data)
        name, args = body.get("name"), body.get("args", {})
        if name not in CALL_NAMES:
            raise WireError(f"a call's 'name' is one of {sorted(CALL_NAMES)}, not {name!r}")
        if not isinstance(args, dict):
            raise WireError("a call's 'args' must be a JSON object")
        return cls(cast("CallName", name), cast("dict[str, Any]", args))


@dataclass(frozen=True)
class Reply:
    """The answer to a call: `ok` with a `body`, or a refusal with a `code` and a
    `message`. The code is the contract; the message is for people."""

    ok: bool
    code: str | None = None
    message: str | None = None
    body: Any = None

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "code": self.code, "message": self.message, "body": self.body}

    @classmethod
    def from_json(cls, data: Any) -> Reply:
        if not isinstance(data, dict):
            raise WireError("a reply must be a JSON object")
        body = cast("dict[str, Any]", data)
        ok = body.get("ok")
        if not isinstance(ok, bool):
            raise WireError("a reply's 'ok' must be true or false")
        code, message = body.get("code"), body.get("message")
        if not ok and not (isinstance(code, str) and isinstance(message, str)):
            raise WireError("a reply that is not ok must have a 'code' and a 'message'")
        return cls(
            ok=ok,
            code=code if isinstance(code, str) else None,
            message=message if isinstance(message, str) else None,
            body=body.get("body"),
        )
