"""`try_workflow`: run a workflow in one process, with the real routines, and stop.

A new SQLite database, a control plane with its clock, the supervisor of the folder
over `InProcess`, and one token. The try-out stops by itself: when the top token ends,
at a state that waits for the world, when nothing changes for `quiet` seconds, or when
`timeout` seconds have gone.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from stateous.control.engine import HistoryEntry, Token
from stateous.control.inprocess import InProcess
from stateous.control.plane import ControlPlane
from stateous.supervisor.run import supervise
from stateous.supervisor.supervisor import SupervisorError
from stateous.tryout.report import PAGE_OF, Reason, Stop, TryReport
from stateous.tryout.stops import waiting_stop
from stateous.tryout.watch import Step, Watching

EVERY = 0.2
"""Seconds between two looks at the tokens."""
GRACE = 5.0
"""Seconds that the running routines get to end after the stop. Then they are cancelled."""
ENDED = ("completed", "failed", "cancelled")
FORM = "give a fire as STATE=EVENT or STATE=EVENT:PAYLOAD_JSON"


@dataclass(frozen=True)
class Fire:
    """An answer for a state that waits: fire `event` (with `payload`) on the token."""

    state: str
    event: str
    payload: dict[str, Any] | None

    @property
    def label(self) -> str:
        return f"{self.state}={self.event}"


def parse_fire(text: str) -> Fire:
    """`STATE=EVENT` or `STATE=EVENT:PAYLOAD_JSON`. ValueError with one sentence if bad."""
    state, equals, rest = text.partition("=")
    event, colon, payload_text = rest.partition(":")
    if not equals or not state or not event:
        raise ValueError(f"{text!r} is not a fire: {FORM}")
    if not colon:
        return Fire(state, event, None)
    try:
        payload: object = json.loads(payload_text)
    except json.JSONDecodeError:
        raise ValueError(f"the payload of {text!r} is not JSON: {FORM}") from None
    if not isinstance(payload, dict):
        raise ValueError(f"the payload of {text!r} is not a JSON object: {FORM}")
    return Fire(state, event, cast("dict[str, Any]", payload))


async def try_workflow(
    graph: Path,
    folder: Path,
    context: dict[str, Any],
    *,
    fires: Sequence[Fire] = (),
    timeout: float = 600,
    quiet: float = 120,
    database: Path | None = None,
) -> TryReport:
    """Run one token of `graph` with the routines of `folder`, and report why it stopped.

    Raises `SupervisorError` for a folder that cannot run, and a `ControlPlaneError`
    for a context or a fire that the control plane refuses."""
    if database is None:
        database = Path(tempfile.mkdtemp(prefix="stateous-try-")) / "try.db"
    with ControlPlane(graph, database=f"sqlite:///{database.as_posix()}", migrate=True) as plane:
        top = plane.start(None, context).token
        watching = Watching(InProcess(plane))
        stopping = asyncio.Event()
        clock = asyncio.create_task(plane.run_clock(every=0.5))
        supervising = asyncio.create_task(supervise(folder, watching, stop=stopping, idle=1))
        left = list(fires)
        used: list[str] = []
        try:
            stop = await _watch(plane, top, watching, supervising, left, used, timeout, quiet)
        finally:
            stopping.set()
            await _end(supervising)
            clock.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await clock
        family, _, histories = _family(plane, top)
        unused = [fire.label for fire in left]
        return TryReport(stop, watching.steps, family, histories, used, unused, database)


async def _watch(
    plane: ControlPlane,
    top: str,
    watching: Watching,
    supervising: asyncio.Task[int],
    left: list[Fire],
    used: list[str],
    timeout: float,
    quiet: float,
) -> Stop:
    """Look at the tokens until a stop. A fire that answers a waiting state is used."""
    started = progressed = time.monotonic()
    seen = 0
    while True:
        if supervising.done():
            supervising.result()  # raises the supervisor's error, if there is one
            raise SupervisorError("the supervisor stopped before the token ended")
        family, workflows, histories = _family(plane, top)
        count = sum(len(entries) for entries in histories.values())
        if count != seen:
            seen, progressed = count, time.monotonic()
        head = family[0]
        if head.status in ENDED:
            reason = cast("Reason", head.status)
            return Stop(reason, top, head.state, _ended(head), PAGE_OF[reason])
        if watching.served and not watching.running():
            waiting = waiting_stop(plane.document, watching.served, family, workflows, histories)
            if waiting is not None:
                fire = _take(left, waiting.state)
                if fire is None:
                    return waiting
                plane.fire(waiting.token, fire.event, fire.payload)
                used.append(fire.label)
                continue
        now = time.monotonic()
        if now - max(watching.last_change, progressed) > quiet:
            return _late("quiet", f"nothing changed for {quiet:g} s", head, watching.running())
        if now - started > timeout:
            sentence = f"the time limit of {timeout:g} s ended"
            return _late("time_limit", sentence, head, watching.running())
        await asyncio.sleep(EVERY)


def _family(
    plane: ControlPlane, top: str
) -> tuple[list[Token], dict[str, str], dict[str, list[HistoryEntry]]]:
    """The top token and its children at every depth; the child workflow of each child;
    and the history of each token."""
    family: list[Token] = []
    workflows: dict[str, str] = {}
    histories: dict[str, list[HistoryEntry]] = {}
    queue = [plane.get(top)]
    while queue:
        token = queue.pop(0)
        family.append(token)
        histories[token.token] = plane.history(token.token)
        for entry in histories[token.token]:
            if entry.kind == "branch_started":
                workflows[str(entry.data["token"])] = str(entry.data["workflow"])
        children = plane.tokens(parent=token.token, limit=100)
        queue.extend(sorted(children, key=lambda child: child.token))
    return family, workflows, histories


def _take(left: list[Fire], state: str) -> Fire | None:
    """The first fire left for `state` (`workflow/state`, or `state` alone), taken out."""
    for index, fire in enumerate(left):
        if fire.state in (state, state.rsplit("/", 1)[-1]):
            return left.pop(index)
    return None


def _ended(head: Token) -> str:
    if head.status == "completed":
        return f"{head.token} completed"
    return f"{head.token} {head.status} at {head.state}"


def _late(reason: Reason, why: str, head: Token, running: list[Step]) -> Stop:
    if running:
        shown = ", ".join(
            f"{step.state} of {step.token} (attempt {step.attempt})" for step in running
        )
        sentence = f"{why}; still running: {shown}"
    else:
        sentence = f"{why}; {head.token} is at {head.state}"
    return Stop(reason, head.token, head.state, sentence, PAGE_OF[reason])


async def _end(supervising: asyncio.Task[int]) -> None:
    """Wait for the supervisor to stop; after `GRACE` seconds, cancel it."""
    await asyncio.wait({supervising}, timeout=GRACE)
    if not supervising.done():
        supervising.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await supervising
