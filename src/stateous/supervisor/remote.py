"""The control plane's HTTP API (contract section 13), as the supervisor calls it:
async, over httpx2.

A call that does not reach the control plane is tried again after 1, 2, 4, 8, 16,
then every 30 seconds, with one log line for each failure, until it gets an
answer. An answer 502, 503 or 504 counts as "not reached" (a proxy, a control
plane that restarts, or a control plane that is `not_serving`: another one can take
over at the same URL); the log line gives its code and sentence, if it has them.
Any other answer of 400 or more is a refusal: `Refused`, and no new try. A refusal
has the code that the answer gives (design section 5.4); read the code, never the
sentence.

A call can get a `stop` event. When it is set, a call that waits to try again
gives up: a claim returns None, and a report, a note, a memory write or a
heartbeat raises `GaveUp`. A call made after the stop has one try. A request in flight is not cut:
the control plane can grant a task in it, and the supervisor must run that task."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection, Sequence
from typing import Any, cast

import httpx2

from stateous.model.wire import Outcome, TaskGrant

log = logging.getLogger("stateous.supervisor")

BACKOFF: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
"""Seconds before each new try. The last value repeats."""

UNAVAILABLE = frozenset({502, 503, 504})
"""Answers that count as "the control plane is not reached"."""


class Refused(Exception):
    """The control plane answered with an error. `status` is the HTTP status,
    `message` the sentence, for people, and `code` the stable code of the refusal.
    `code` is None when the answer has no code, for example an answer from a proxy,
    or an answer that the supervisor cannot read."""

    def __init__(self, status: int, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


class ControlPlaneApi:
    """The control plane, reached over HTTP."""

    def __init__(
        self,
        url: str,
        *,
        client: httpx2.AsyncClient | None = None,
        timeout: float = 30.0,
        backoff: Sequence[float] = BACKOFF,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.url = url.rstrip("/")
        self._client = (
            client if client is not None else httpx2.AsyncClient(base_url=self.url, timeout=timeout)
        )
        self._backoff = tuple(backoff) or BACKOFF
        self._sleep = sleep

    async def health(self) -> dict[str, Any]:
        """`{"workflow", "checksum"}` of the graph the control plane serves."""
        return _mapping(await self._call("GET", "/health"))

    async def claim(
        self,
        supervisor: str,
        states: Collection[str],
        *,
        stop: asyncio.Event | None = None,
        request: str | None = None,
    ) -> TaskGrant | None:
        """A task of one of `states`, or None when there is none now. Also None when
        `stop` is set while the claim waits to try again. Each new try sends the same
        `request` id: when the control plane granted a task but its answer was lost,
        the next try gets that task again."""
        body: dict[str, Any] = {"supervisor": supervisor, "states": list(states)}
        if request is not None:
            body["request"] = request
        try:
            response = await self._call("POST", "/tasks/claim", body, stop=stop)
        except GaveUp:
            return None
        if response.status_code == 204:
            return None
        try:
            return TaskGrant.from_json(_body(response))
        except ValueError as error:
            raise Refused(response.status_code, f"the claimed task is not valid: {error}") from None

    async def report(
        self, task: int, outcome: Outcome, *, supervisor: str, stop: asyncio.Event | None = None
    ) -> None:
        """Report a task. The outcome names the attempt it is for."""
        body = {**outcome.to_json(), "supervisor": supervisor}
        await self._call("POST", f"/tasks/{task}/report", body, stop=stop)

    async def heartbeat(
        self, supervisor: str, tasks: Collection[int], *, stop: asyncio.Event | None = None
    ) -> list[int]:
        """The ids among `tasks` that this supervisor no longer holds."""
        body = {"supervisor": supervisor, "tasks": list(tasks)}
        answer = _mapping(await self._call("POST", "/tasks/heartbeat", body, stop=stop))
        lost = answer.get("lost")
        if not isinstance(lost, list):
            raise Refused(200, "the heartbeat answer has no list of lost tasks")
        return [int(task) for task in cast("list[Any]", lost)]

    async def note(
        self, task: int, text: str, *, attempt: int, stop: asyncio.Event | None = None
    ) -> None:
        """One line of progress, for that attempt of the task."""
        body = {"text": text, "attempt": attempt}
        await self._call("POST", f"/tasks/{task}/note", body, stop=stop)

    async def set_memory(
        self,
        task: int,
        key: str,
        value: Any,
        *,
        attempt: int,
        stop: asyncio.Event | None = None,
    ) -> None:
        """Save one key of the routine's memory, for that attempt of the task. `None`
        deletes the key."""
        body = {"key": key, "value": value, "attempt": attempt}
        await self._call("PUT", f"/tasks/{task}/memory", body, stop=stop)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(
        self, method: str, path: str, body: Any = None, *, stop: asyncio.Event | None = None
    ) -> httpx2.Response:
        failures = 0
        while True:
            try:
                response = await self._client.request(method, path, json=body)
            except httpx2.TransportError as error:
                problem = _unreached(self.url, str(error) or type(error).__name__)
            else:
                if response.status_code in UNAVAILABLE:
                    problem = _unavailable(self.url, response)
                elif response.status_code >= 400:
                    message, code = _error_of(response)
                    raise Refused(response.status_code, message, code=code)
                else:
                    return response
            if stop is not None and stop.is_set():
                raise GaveUp
            delay = self._backoff[min(failures, len(self._backoff) - 1)]
            failures += 1
            log.warning("%s; trying again in %gs", problem, delay)
            await self._wait(delay, stop)

    async def _wait(self, delay: float, stop: asyncio.Event | None) -> None:
        """Sleep `delay` seconds. Raise `GaveUp` when `stop` is set first."""
        if stop is None:
            await self._sleep(delay)
            return
        sleeping = asyncio.ensure_future(self._sleep(delay))
        stopping = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({sleeping, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            if not sleeping.done():
                sleeping.cancel()
        if stop.is_set():
            raise GaveUp
        await sleeping


class GaveUp(Exception):
    """A call did not reach the control plane, and stopped to try again: its `stop`
    event is set."""


def _error_of(response: httpx2.Response) -> tuple[str, str | None]:
    """The `error` sentence of an error answer, else its text, on one line; and the
    answer's `code`, or None when it has none."""
    try:
        data: Any = response.json()
    except ValueError:
        data = None
    body = cast("dict[str, Any]", data) if isinstance(data, dict) else {}
    error, code = body.get("error"), body.get("code")
    named = code if isinstance(code, str) else None
    if isinstance(error, str) and error:
        return error, named
    text = " ".join(response.text.split())[:200]
    return text or f"the control plane answered {response.status_code}", named


def _unreached(url: str, why: str) -> str:
    """The start of the log line of a call that did not reach the control plane."""
    return f"cannot reach the control plane at {url} ({why})"


def _unavailable(url: str, response: httpx2.Response) -> str:
    """The start of the log line of an answer 502, 503 or 504, which counts as "not
    reached". When the answer has a code, the line gives the code and the sentence.
    `not_serving` (another control plane took the database) is not an outage: the
    line says that the control plane does not serve its database now, and gives the
    code. Another control plane can take over at the same URL, so the supervisor
    tries again."""
    message, code = _error_of(response)
    if code == "not_serving":
        return (
            f"the control plane at {url} does not serve its database now (not_serving): {message}"
        )
    if code is None:
        return _unreached(url, f"it answered {response.status_code}")
    return _unreached(url, f"it answered {response.status_code}, {code}: {message}")


def _body(response: httpx2.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise Refused(response.status_code, "the control plane's answer is not JSON") from None


def _mapping(response: httpx2.Response) -> dict[str, Any]:
    data = _body(response)
    if not isinstance(data, dict):
        raise Refused(response.status_code, "the control plane's answer is not a JSON object")
    return cast("dict[str, Any]", data)
