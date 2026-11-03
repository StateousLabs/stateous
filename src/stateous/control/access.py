"""Who may do what: the actions that a developer's authorizer decides on.

Stateous never knows what a user, a tenant or a plan is. Each call of a control plane
gives the authorizer the caller (an opaque value from the transport, for example a
user id that a gateway checked), the action, and the labels of the tokens it touches.
The authorizer answers True or False."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal

Action = Literal[
    "claim", "report", "heartbeat", "note", "memory",
    "start", "fire", "pause", "resume", "cancel", "read",
]  # fmt: skip
"""What a caller does: a supervisor's call, a token's start or steering, or a read."""

Authorize = Callable[[object, Action, Mapping[str, str]], bool]
"""The developer's function: `authorize(caller, action, labels)`. True allows."""
