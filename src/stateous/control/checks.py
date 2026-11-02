"""The checks of the arguments of a request, for the HTTP API and for `answer`. Each
refusal is a `Problem` with its status, one plain sentence and its code."""

from __future__ import annotations

import re
from typing import Any, cast

from stateous.control.engine import REQUEST_PATTERN


class Problem(Exception):
    """A request that the API refuses, with the status, one plain sentence and the
    code of the refusal (see the docs, Reference > The HTTP API). The code is `bad_request`
    when the request names none."""

    def __init__(self, status: int, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


MAX_NAME = 128
"""The longest memory key."""


MAX_STATE = 129
"""The longest state name in a claim: `<workflow>/<state>`, 64 + 1 + 64 characters."""


MAX_ITEMS = 1000
"""The most state names in a claim, and the most task ids in a heartbeat."""


MAX_SUPERVISOR = 256
"""The longest supervisor name: the engine's own limit."""


MAX_NOTE = 500
"""The longest note, after the spaces at its two ends are removed."""


MAX_TASK = 2**63 - 1
"""The largest task id that a database integer holds."""


def name(value: Any, what: str, limit: int = MAX_NAME, *, code: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise Problem(422, f"'{what}' is a string of 1 to {limit} characters", code)
    return value


def supervisor_name(value: Any) -> str:
    """A supervisor's name, as the engine takes it; else 422 `invalid_argument`."""
    return name(value, "supervisor", MAX_SUPERVISOR, code="invalid_argument")


def state_names(value: Any, what: str) -> list[str]:
    items = cast("list[Any]", value) if isinstance(value, list) else None
    if items is None or not all(isinstance(i, str) and 1 <= len(i) <= MAX_STATE for i in items):
        raise Problem(422, f"'{what}' is a list of state names", "invalid_body")
    if len(items) > MAX_ITEMS:
        raise Problem(422, f"'{what}' has at most {MAX_ITEMS} state names", "invalid_argument")
    return cast("list[str]", items)


def request_id(value: Any) -> str | None:
    """A claim's request id; None when the body names none."""
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(REQUEST_PATTERN, value):
        raise Problem(
            422, "'request' is 1 to 64 letters, digits, '.', '_' or '-'", "invalid_argument"
        )
    return value


def task_ids(value: Any) -> list[int]:
    items = cast("list[Any]", value) if isinstance(value, list) else None
    if items is None or not all(
        isinstance(i, int) and not isinstance(i, bool) and 0 < i <= MAX_TASK for i in items
    ):
        raise Problem(422, "'tasks' is a list of task ids", "invalid_body")
    if len(items) > MAX_ITEMS:
        raise Problem(422, f"'tasks' has at most {MAX_ITEMS} task ids", "invalid_argument")
    return cast("list[int]", items)


def attempt(value: Any) -> int | None:
    """The attempt a request is for; None when the body names none."""
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= MAX_TASK:
        raise Problem(422, "'attempt' is a whole number of 1 or more", "invalid_argument")
    return value


def note_text(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not 1 <= len(text) <= MAX_NOTE:
        raise Problem(
            422, f"a note needs a 'text' of 1 to {MAX_NOTE} characters", "invalid_payload"
        )
    return text
