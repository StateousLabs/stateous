"""Durations, as `timeout=` and `ctx.schedule(after=...)` take them: '30s', '10m', '6h', '1d'.

One rule for every duration in Stateous, so the compiler, the wire and the engine
agree on it."""

from __future__ import annotations

import re
from datetime import timedelta

DURATION = re.compile(r"^[1-9][0-9]*[smhd]$")
"""A whole number, 1 or more, and a unit: s, m, h or d."""

MAX_DURATION_DAYS = 3650
"""The longest duration Stateous takes. A longer one is surely a mistake, and a
date that far off cannot always be stored."""

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def duration_problem(text: str) -> str | None:
    """Why `text` is not a duration Stateous takes, or None when it is."""
    is_text = isinstance(text, str)  # pyright: ignore[reportUnnecessaryIsInstance]
    if not is_text or DURATION.fullmatch(text) is None:
        return f"{text!r} is not a duration; write e.g. '30s', '10m', '2h', '1d'"
    if int(text[:-1]) * _UNIT_SECONDS[text[-1]] > MAX_DURATION_DAYS * 86400:
        return f"{text!r} is too long; a duration is at most {MAX_DURATION_DAYS}d"
    return None


def parse_duration(text: str) -> timedelta:
    """The duration `text` names. `ValueError` with the reason when it is not one."""
    problem = duration_problem(text)
    if problem is not None:
        raise ValueError(problem)
    return timedelta(seconds=int(text[:-1]) * _UNIT_SECONDS[text[-1]])
