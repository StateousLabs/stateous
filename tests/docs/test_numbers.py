"""Each number that a page says comes from a constant of the code.

The expected text is made from the constant. So when a constant changes, the pages
that say the old number fail here, and you change them.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from stateous.control.api import MAX_BODY, MAX_DEPTH
from stateous.control.engine import (
    DEFAULT_LEASE,
    KEY_LENGTH,
    MEMORY_KEY_LENGTH,
    NOTE_LENGTH,
    SUPERVISOR_LENGTH,
)
from stateous.control.store.store import (
    CANDIDATES,
    POSTGRES_CONNECT_WITHIN,
    POSTGRES_KEEPALIVES,
    POSTGRES_LOCK_WAIT,
    POSTGRES_STATEMENT_WITHIN,
    SERVING_LEASE,
    SQLITE_WAIT,
)
from stateous.model.durations import MAX_DURATION_DAYS
from stateous.model.workflow import MAX_TOKEN_ID
from stateous.supervisor.folder import DEFAULT_TASKS, MAX_TASKS, FolderConfig
from stateous.supervisor.remote import BACKOFF
from stateous.supervisor.supervisor import HEARTBEAT_EVERY

from .pages import page


def minutes(span: timedelta) -> str:
    count = int(span.total_seconds() // 60)
    return f"{count} minute" if count == 1 else f"{count} minutes"


def seconds(value: float) -> str:
    return f"{int(value)} seconds"


def listed(numbers: list[str]) -> str:
    """`1, 2, 4, 8 and 16`."""
    return ", ".join(numbers[:-1]) + " and " + numbers[-1]


LEASE = minutes(DEFAULT_LEASE)
EARLY = [str(int(delay)) for delay in BACKOFF[:-1]]
LAST = seconds(BACKOFF[-1])
MIB = MAX_BODY // 2**20
NAME_IN_YAML = 128
"""`name` in `stateous.yaml`: the `max_length` of its field."""

SAID: dict[str, list[str]] = {
    "concepts/tasks-and-routines.md": [
        f"a lease of {LEASE}",
        f"a heartbeat every {seconds(HEARTBEAT_EVERY)}",
        f"no heartbeat for {LEASE}",
        f"up to {seconds(HEARTBEAT_EVERY)} later",
    ],
    "concepts/model.md": [f"every {seconds(HEARTBEAT_EVERY)} for all the tasks"],
    "concepts/reliability.md": [
        f"a lease of {LEASE}",
        f"no heartbeat for {LEASE}",
        f"after {listed(EARLY)} seconds, then every {LAST}",
        f"| `statement_timeout` | {seconds(POSTGRES_STATEMENT_WITHIN)} |",
        f"up to {seconds(SQLITE_WAIT)} for the write lock",
    ],
    "concepts/context-notes-memory.md": [
        f"up to {NOTE_LENGTH} characters",
        f"1 to {MEMORY_KEY_LENGTH} characters",
    ],
    "concepts/timers.md": [f"The longest is `{MAX_DURATION_DAYS}d`"],
    "how-to/write-a-workflow.md": [f"The longest is `{MAX_DURATION_DAYS}d`"],
    "how-to/run-supervisors.md": [
        f"after {listed(EARLY)} seconds, then every {LAST}",
        f"`tasks` is 1 to {MAX_TASKS}",
        f"{LEASE} after the last heartbeat",
    ],
    "how-to/use-postgres.md": [
        f"| `connect_timeout` | {seconds(POSTGRES_CONNECT_WITHIN)} |",
        f"| `statement_timeout` | {seconds(POSTGRES_STATEMENT_WITHIN)} |",
        f"| `lock_timeout` | {seconds(POSTGRES_LOCK_WAIT)} |",
        f"a probe after {seconds(POSTGRES_KEEPALIVES['keepalives_idle'])}, then every "
        f"{seconds(POSTGRES_KEEPALIVES['keepalives_interval'])}, "
        f"{POSTGRES_KEEPALIVES['keepalives_count']} probes",
        f"waits up to {seconds(SQLITE_WAIT)} for the write lock",
        f"a new one can start after {seconds(SERVING_LEASE.total_seconds())}",
    ],
    "how-to/steer-tokens.md": [f"from 1 to {KEY_LENGTH} characters"],
    "inside/workflows.md": [
        f"the {CANDIDATES} oldest queued tasks",
        f"sets a lease of {LEASE}",
        f"no heartbeat for {LEASE}",
        f"then {', '.join(EARLY[1:])} and {LAST}",
        f"more than {SUPERVISOR_LENGTH} characters",
    ],
    "reference/cli.md": [f"from 1 to {KEY_LENGTH} characters"],
    "reference/files.md": [
        f"1 to {MAX_TASKS}. The default is {DEFAULT_TASKS}.",
        f"1 to {NAME_IN_YAML} characters",
    ],
    "reference/http.md": [
        f"at most {MIB} MiB, and it can nest at most {MAX_DEPTH} levels",
        f"1 to {SUPERVISOR_LENGTH} characters",
        f"1 to {NOTE_LENGTH} characters",
        f"1 to {MEMORY_KEY_LENGTH} characters",
        f"from 1 to {KEY_LENGTH} characters",
    ],
    "reference/limits.md": [
        f"| A request body | {MIB} MiB |",
        f"| The nesting of a body's JSON | {MAX_DEPTH} levels |",
        f"| A token id | {MAX_TOKEN_ID} characters |",
        f"| An idempotency key | {KEY_LENGTH} characters |",
        f"| A supervisor's name | {SUPERVISOR_LENGTH} characters in a claim; "
        f"{NAME_IN_YAML} characters in `stateous.yaml` |",
        f"| A note | {NOTE_LENGTH} characters |",
        f"| A memory key | {MEMORY_KEY_LENGTH} characters |",
        f"| The routines that one supervisor runs at once | {MAX_TASKS} |",
    ],
}


def test_the_name_limit_in_stateous_yaml_is_the_one_the_pages_say() -> None:
    rules = FolderConfig.model_fields["name"].metadata
    limits = [getattr(rule, "max_length", None) for rule in rules]
    assert NAME_IN_YAML in limits


@pytest.mark.parametrize("name", sorted(SAID))
def test_the_page_says_the_numbers_of_the_code(name: str) -> None:
    text = page(name)
    for said in SAID[name]:
        assert said in text, (name, said)
