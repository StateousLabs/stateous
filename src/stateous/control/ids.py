"""Ids that Stateous makes: ULIDs in lower case. 26 characters, the time of creation
first, so ids made later sort later, also within one millisecond in one process."""

from __future__ import annotations

import os
import threading
import time

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
"""Crockford's base 32, in lower case: no i, l, o or u."""

_RANDOM_BITS = 80
_lock = threading.Lock()
_last = (0, 0)


def new_id() -> str:
    """A new ULID: 48 bits of milliseconds, then 80 bits of randomness. Within one
    millisecond, the randomness of the last id goes up by one, so the order holds."""
    global _last
    with _lock:
        millis = time.time_ns() // 1_000_000
        last_millis, last_random = _last
        if millis <= last_millis:
            millis, random = last_millis, last_random + 1
        else:
            random = int.from_bytes(os.urandom(_RANDOM_BITS // 8), "big")
        _last = (millis, random)
    value = (millis << _RANDOM_BITS) | (random & ((1 << _RANDOM_BITS) - 1))
    return "".join(_ALPHABET[(value >> shift) & 31] for shift in range(125, -1, -5))
