"""`supervise`: run the supervisor of a folder over any transport, from Python.

A desktop application, a service, or a test calls it with its own transport: HTTP
through a gateway, a queue, or the control plane in the same process."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from pathlib import Path

from stateous.supervisor.folder import FolderError, load_folder
from stateous.supervisor.retrying import Retrying
from stateous.supervisor.supervisor import IDLE, SupervisorError, open_supervisor
from stateous.transport import Transport


async def supervise(
    folder: str | Path,
    transport: Transport,
    *,
    stop: asyncio.Event | None = None,
    idle: float = IDLE,
    labels: Mapping[str, str] | None = None,
) -> int:
    """Run the supervisor of `folder` over `transport`, until `stop` is set (then it
    stops as on Ctrl-C: the running routines end and report) or until it ends.
    Returns its exit code. A folder that cannot run raises `SupervisorError` with one
    sentence, for example a contract for another version of the workflow.

    `idle` is how long a claim may wait for a task at the control plane (at most 30
    seconds), and the time between two claims when there is none. A longer `idle`
    sends fewer claims; a stop then can wait up to `idle` for the claim in flight.

    `labels` replace the `labels` of the folder's `stateous.yaml`: each claim takes
    only a task whose token has each of them."""
    try:
        loaded = load_folder(Path(folder))
    except FolderError as error:
        raise SupervisorError(str(error)) from None
    supervisor = await open_supervisor(loaded, Retrying(transport), idle=idle, labels=labels)
    watcher: asyncio.Task[None] | None = None
    if stop is not None:

        async def stop_when_set() -> None:
            await stop.wait()
            supervisor.on_signal()

        watcher = asyncio.create_task(stop_when_set())
    try:
        return await supervisor.run()
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        supervisor.close()
