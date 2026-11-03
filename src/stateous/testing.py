"""`check_transport`: a test kit that proves a transport keeps the rules of Stateous.

Write a function that connects your transport to a control plane, then run the kit in
a test::

    def test_my_queue_transport() -> None:
        asyncio.run(check_transport(connect))

The kit makes a small control plane on SQLite for each check, in a folder of its own,
calls `connect(plane)`, and runs the check. Your connect function also starts the
control plane's side of your transport: for a queue, a consumer that takes each
`Call`, runs `answer(plane, call)` and puts back its `Reply`."""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from stateous.control.errors import HoldLost
from stateous.control.plane import ControlPlane
from stateous.model.wire import Outcome
from stateous.transport import NotReached, Refused, Transport

Connect = Callable[[ControlPlane], Awaitable[Transport]]
"""Your function: the transport to `plane`, ready to use."""

KIT_GRAPH: dict[str, Any] = {
    "stateous": 1,
    "workflow": "kit",
    "context": "Job",
    "states": {"work": {"retries": 1, "emits": ["done"]}},
    "events": {"done": {}},
    "transitions": [
        {"from": "__start__", "to": "work"},
        {"from": "work", "to": "__end__", "on": "done"},
    ],
    "schemas": {"Job": {"type": "object", "properties": {"title": {"type": "string"}}}},
}
"""One state, `work`, with one retry, and one event, `done`."""

SUPERVISOR = "kit-supervisor"


async def check_transport(connect: Connect) -> None:
    """Run every check on the transport that `connect` gives. Raises one
    `AssertionError` that names each failed check, with why; returns when all pass."""
    failed: list[str] = []
    for name, check in CHECKS.items():
        with tempfile.TemporaryDirectory(prefix="stateous-kit-") as folder:
            url = f"sqlite:///{(Path(folder) / 'kit.db').as_posix()}"
            plane = ControlPlane(KIT_GRAPH, database=url, migrate=True)
            try:
                transport = await connect(plane)
                try:
                    await check(plane, transport, url)
                finally:
                    await transport.aclose()
            except Exception as error:
                failed.append(f"{name}: {type(error).__name__}: {error}")
            finally:
                plane.close()
    if failed:
        raise AssertionError("the transport failed these checks:\n" + "\n".join(failed))


async def _health(plane: ControlPlane, transport: Transport, url: str) -> None:
    health = await transport.health()
    assert health == plane.health(), f"health is {health}, not {plane.health()}"


async def _claim_and_report(plane: ControlPlane, transport: Transport, url: str) -> None:
    plane.start("K-1", {"title": "one"})
    grant = await transport.claim(SUPERVISOR, ["work"])
    assert grant is not None, "a claim got no task, but one was queued"
    await transport.note(grant.task, "working", attempt=grant.attempt)
    await transport.set_memory(grant.task, "session", "s-1", attempt=grant.attempt)
    outcome = Outcome(kind="event", event="done", attempt=grant.attempt)
    await transport.report(grant.task, outcome, supervisor=SUPERVISOR)
    assert plane.get("K-1").status == "completed", "the report did not move the token"


async def _same_request_same_grant(plane: ControlPlane, transport: Transport, url: str) -> None:
    plane.start("K-1", {"title": "one"})
    first = await transport.claim(SUPERVISOR, ["work"], request="kit-r1")
    second = await transport.claim(SUPERVISOR, ["work"], request="kit-r1")
    assert first is not None, "a claim got no task, but one was queued"
    assert second == first, (
        "a claim sent again with the same request id got another answer; "
        "the transport must carry the request id"
    )


async def _refusal_keeps_its_code(plane: ControlPlane, transport: Transport, url: str) -> None:
    try:
        await transport.report(999_999, Outcome(kind="waiting"), supervisor=SUPERVISOR)
    except Refused as refused:
        assert refused.code == "task_not_found", f"the code is {refused.code!r}"
    else:
        raise AssertionError("a report on an unknown task was not refused")


async def _late_attempt_refused(plane: ControlPlane, transport: Transport, url: str) -> None:
    plane.start("K-1", {"title": "one"})
    first = await transport.claim(SUPERVISOR, ["work"])
    assert first is not None, "a claim got no task, but one was queued"
    failed = Outcome(kind="failed", error="kit", attempt=first.attempt)
    await transport.report(first.task, failed, supervisor=SUPERVISOR)
    second = await transport.claim(SUPERVISOR, ["work"])
    assert second is not None and second.attempt == first.attempt + 1, "no second attempt"
    try:
        late = Outcome(kind="event", event="done", attempt=first.attempt)
        await transport.report(first.task, late, supervisor=SUPERVISOR)
    except Refused as refused:
        assert refused.code == "attempt_over", f"the code is {refused.code!r}"
    else:
        raise AssertionError("a report for an old attempt was not refused")


async def _heartbeat_lists_lost_tasks(plane: ControlPlane, transport: Transport, url: str) -> None:
    plane.start("K-1", {"title": "one"})
    grant = await transport.claim(SUPERVISOR, ["work"])
    assert grant is not None, "a claim got no task, but one was queued"
    lost = await transport.heartbeat(SUPERVISOR, [grant.task, 999_999])
    assert lost == [999_999], f"the heartbeat answered {lost}, not [999999]"


async def _note_keeps_its_attempt(plane: ControlPlane, transport: Transport, url: str) -> None:
    plane.start("K-1", {"title": "one"})
    first = await transport.claim(SUPERVISOR, ["work"])
    assert first is not None, "a claim got no task, but one was queued"
    failed = Outcome(kind="failed", error="kit", attempt=first.attempt)
    await transport.report(first.task, failed, supervisor=SUPERVISOR)
    second = await transport.claim(SUPERVISOR, ["work"])
    assert second is not None, "no second attempt"
    try:
        await transport.note(first.task, "late", attempt=first.attempt)
    except Refused as refused:
        assert refused.code == "attempt_over", f"the code is {refused.code!r}"
    else:
        raise AssertionError("a note for an old attempt was not refused; carry `attempt`")


async def _report_keeps_its_supervisor(plane: ControlPlane, transport: Transport, url: str) -> None:
    plane.start("K-1", {"title": "one"})
    grant = await transport.claim(SUPERVISOR, ["work"])
    assert grant is not None, "a claim got no task, but one was queued"
    outcome = Outcome(kind="event", event="done", attempt=grant.attempt)
    try:
        await transport.report(grant.task, outcome, supervisor="another-supervisor")
    except Refused as refused:
        assert refused.code == "task_not_held", f"the code is {refused.code!r}"
    else:
        raise AssertionError(
            "a report by a supervisor that does not hold the task was not refused; "
            "carry `supervisor`"
        )


async def _not_serving_is_not_reached(plane: ControlPlane, transport: Transport, url: str) -> None:
    _take_the_hold(url)
    with contextlib.suppress(HoldLost):  # the plane learns that it lost the database
        plane.tick()
    try:
        await transport.health()
    except NotReached:
        return
    raise AssertionError(
        "a control plane that lost its database answered; "
        "a reply with a code of RETRY_CODES must raise NotReached"
    )


async def _wait_is_bounded(plane: ControlPlane, transport: Transport, url: str) -> None:
    started = time.monotonic()
    grant = await asyncio.wait_for(transport.claim(SUPERVISOR, ["work"], wait=0.2), 5)
    assert grant is None, "a claim got a task, but none was queued"
    assert time.monotonic() - started < 5, "a claim with wait=0.2 took 5 seconds or more"


def _take_the_hold(url: str) -> None:
    """Put another control plane's name in the hold row, as if it took the database."""
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE control_plane SET serving = 'elsewhere:1:kit', since = :now"),
                {"now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")},
            )
    finally:
        engine.dispose()


CHECKS: dict[str, Callable[[ControlPlane, Transport, str], Awaitable[None]]] = {
    "health": _health,
    "claim_and_report": _claim_and_report,
    "same_request_same_grant": _same_request_same_grant,
    "refusal_keeps_its_code": _refusal_keeps_its_code,
    "late_attempt_refused": _late_attempt_refused,
    "heartbeat_lists_lost_tasks": _heartbeat_lists_lost_tasks,
    "note_keeps_its_attempt": _note_keeps_its_attempt,
    "report_keeps_its_supervisor": _report_keeps_its_supervisor,
    "not_serving_is_not_reached": _not_serving_is_not_reached,
    "wait_is_bounded": _wait_is_bounded,
}
"""Each check, by the name that a failure gives."""
