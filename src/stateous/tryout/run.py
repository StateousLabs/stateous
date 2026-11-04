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
from stateous.control.errors import ControlPlaneError
from stateous.control.inprocess import InProcess
from stateous.control.plane import ControlPlane
from stateous.supervisor.folder import FolderError, load_folder
from stateous.supervisor.retrying import Retrying
from stateous.supervisor.supervisor import Supervisor, SupervisorError, open_supervisor
from stateous.tryout.report import PAGE_OF, Reason, Stop, TryReport
from stateous.tryout.stops import leaving, waiting_stop
from stateous.tryout.watch import Step, Watching

EVERY = 0.2
"""Seconds between two looks at the tokens."""
GRACE = 5.0
"""Seconds that the running routines get to end after the stop. Then the supervisor
cancels them, and after GRACE again its task is cancelled."""
ENDED = ("completed", "failed", "cancelled")
FORM = "give a fire as STATE=EVENT, STATE=EVENT:PAYLOAD_JSON or STATE=EVENT:@FILE"


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
    """`STATE=EVENT`, `STATE=EVENT:PAYLOAD_JSON`, or `STATE=EVENT:@FILE` for a payload in a
    JSON file. ValueError with one sentence if bad."""
    state, equals, rest = text.partition("=")
    event, colon, payload_text = rest.partition(":")
    if not equals or not state or not event:
        raise ValueError(f"{text!r} is not a fire: {FORM}")
    if not colon:
        return Fire(state, event, None)
    if payload_text.startswith("@"):
        try:
            payload_text = Path(payload_text[1:]).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise ValueError(f"cannot read the payload file of {text!r}: {FORM}") from None
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

    The supervisor uses an outbox next to the database, never the folder's own: so the
    messages that a real supervisor left in the folder stay there. Raises ValueError for
    a fire that names a state or an event that the graph does not have,
    `SupervisorError` for a folder that cannot run, and a `ControlPlaneError` for a
    context that the control plane refuses."""
    if database is None:
        database = Path(tempfile.mkdtemp(prefix="stateous-try-")) / "try.db"
    outbox = database.with_name(f"{database.stem}-outbox.db")
    with ControlPlane(graph, database=f"sqlite:///{database.as_posix()}", migrate=True) as plane:
        check_fires(plane.document, fires)
        top = plane.start(None, context).token
        watching = Watching(InProcess(plane), where=str(graph))
        clock = asyncio.create_task(plane.run_clock(every=0.5))
        try:
            supervisor = await _open(folder, watching, outbox)
        except BaseException:
            await _cancel(clock)
            raise
        supervising = asyncio.create_task(supervisor.run())
        left = list(fires)
        used: list[str] = []
        try:
            stop = await _watch(plane, top, watching, supervising, left, used, timeout, quiet)
        finally:
            await _end(supervisor, supervising)
            supervisor.close()
            await _cancel(clock)
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
                try:
                    plane.fire(waiting.token, fire.event, fire.payload)
                except ControlPlaneError as error:
                    return _refused(fire, waiting, error)
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


def check_fires(document: dict[str, Any], fires: Sequence[Fire]) -> None:
    """Refuse a fire whose state is not in the graph, or whose event does not leave
    that state. ValueError with one sentence. A guard can still refuse a fire at run time.

    A state is `state` of the top workflow or of any child workflow, or `workflow/state`."""
    children = cast("dict[str, dict[str, Any]]", document.get("workflows", {}))
    for fire in fires:
        workflow, slash, state = fire.state.rpartition("/")
        if slash:
            bodies = [children[workflow]] if workflow in children else []
        else:
            bodies = [document, *children.values()]
        bodies = [body for body in bodies if state in body["states"]]
        if not bodies:
            raise ValueError(f"--fire {fire.label}: the graph has no state {fire.state!r}")
        events = sorted({event for body in bodies for event in leaving(body, state)})
        if fire.event not in events:
            shown = ", ".join(events) or "none"
            raise ValueError(
                f"--fire {fire.label}: {fire.event!r} does not leave {fire.state!r} "
                f"(events: {shown})"
            )


async def _open(folder: Path, watching: Watching, outbox: Path) -> Supervisor:
    """The supervisor of `folder`, over `watching`, with its outbox at `outbox`."""
    try:
        loaded = load_folder(folder)
    except FolderError as error:
        raise SupervisorError(str(error)) from None
    return await open_supervisor(loaded, Retrying(watching), idle=1, outbox_file=outbox)


def _refused(fire: Fire, waiting: Stop, error: ControlPlaneError) -> Stop:
    sentence = (
        f"the control plane refused --fire {fire.label} on {waiting.token}: {error}; "
        f"before that: {waiting.sentence}"
    )
    return Stop("fire_refused", waiting.token, waiting.state, sentence, PAGE_OF["fire_refused"])


async def _end(supervisor: Supervisor, supervising: asyncio.Task[int]) -> None:
    """Stop the supervisor: the running routines get `GRACE` seconds to end; then the
    supervisor cancels them; after `GRACE` again, its task is cancelled."""
    supervisor.on_signal()
    await asyncio.wait({supervising}, timeout=GRACE)
    if not supervising.done():
        supervisor.stop_now()
        await asyncio.wait({supervising}, timeout=GRACE)
    await _cancel(supervising)


async def _cancel(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
