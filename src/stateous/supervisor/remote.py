"""`HttpTransport`: the control plane's HTTP API (see the docs, Reference > The HTTP API)
as a `Transport`, over httpx2. Each call makes one attempt; `Retrying` tries again.

An answer 408, 429, 502, 503 or 504, an answer with a code of `RETRY_CODES`,
headers that cannot be made, or no answer at all is `NotReached`: a gateway that says
"later", a control plane that restarts, or one that is `not_serving` (another one can
take over at the same URL). Any other answer of 400
or more is `Refused`, with the code that the answer gives. An answer with no code
(for example from a gateway) is refused as `unexpected`, and its message ends with
the HTTP status."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import Any, cast

import httpx2

from stateous.model.wire import Outcome, TaskGrant
from stateous.transport import RETRY_CODES, NotReached, Refused

UNAVAILABLE = frozenset({408, 429, 502, 503, 504})
"""Answers that count as "the control plane is not reached": a gateway's timeout or
rate limit, a bad gateway, a control plane that restarts."""


class HttpTransport:
    """The control plane, reached over HTTP. `url` can have a path, for a control plane
    behind a gateway: `https://gateway.example/stateous/plan`. `headers` is called for
    each request, so a bearer token can renew itself."""

    def __init__(
        self,
        url: str,
        *,
        headers: Callable[[], Mapping[str, str]] | None = None,
        client: httpx2.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.where = self.url
        self._headers = headers
        self._timeout = timeout
        self._client = (
            client if client is not None else httpx2.AsyncClient(base_url=self.url, timeout=timeout)
        )

    async def health(self) -> dict[str, Any]:
        """`{"workflow", "checksum"}` of the graph the control plane serves."""
        return _mapping(await self._call("GET", "health"))

    async def claim(
        self,
        supervisor: str,
        states: Collection[str],
        *,
        request: str | None = None,
        wait: float = 0,
        labels: Mapping[str, str] | None = None,
    ) -> TaskGrant | None:
        """A task of one of `states`, or None. With `wait`, the control plane may hold
        the request up to `wait` seconds for a task. With `labels`: only a task whose
        token has each of them."""
        body: dict[str, Any] = {"supervisor": supervisor, "states": list(states)}
        if labels:
            body["labels"] = dict(labels)
        if request is not None:
            body["request"] = request
        if wait > 0:
            body["wait"] = wait
        response = await self._call("POST", "tasks/claim", body, timeout=self._timeout + wait)
        if response.status_code == 204:
            return None
        try:
            return TaskGrant.from_json(_body(response))
        except ValueError as error:
            raise Refused("unexpected", f"the claimed task is not valid: {error}") from None

    async def report(self, task: int, outcome: Outcome, *, supervisor: str) -> None:
        """Report a task. The outcome names the attempt it is for."""
        body = {**outcome.to_json(), "supervisor": supervisor}
        await self._call("POST", f"tasks/{task}/report", body)

    async def heartbeat(self, supervisor: str, tasks: Collection[int]) -> list[int]:
        """The ids among `tasks` that this supervisor no longer holds."""
        body = {"supervisor": supervisor, "tasks": list(tasks)}
        answer = _mapping(await self._call("POST", "tasks/heartbeat", body))
        lost = answer.get("lost")
        if not isinstance(lost, list):
            raise Refused("unexpected", "the heartbeat answer has no list of lost tasks")
        return [int(task) for task in cast("list[Any]", lost)]

    async def note(self, task: int, text: str, *, attempt: int) -> None:
        """One line of progress, for that attempt of the task."""
        await self._call("POST", f"tasks/{task}/note", {"text": text, "attempt": attempt})

    async def set_memory(self, task: int, key: str, value: Any, *, attempt: int) -> None:
        """Save one key of the routine's memory. `None` deletes the key."""
        body = {"key": key, "value": value, "attempt": attempt}
        await self._call("PUT", f"tasks/{task}/memory", body)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(
        self, method: str, path: str, body: Any = None, *, timeout: float | None = None
    ) -> httpx2.Response:
        """One request. `path` has no leading `/`, so the URL's own path stays."""
        try:
            headers = dict(self._headers()) if self._headers is not None else None
        except Exception as error:  # e.g. a token that cannot be renewed now
            raise NotReached(
                f"cannot make the headers for {self.url}: {type(error).__name__}: {error}"
            ) from None
        try:
            if timeout is None:
                response = await self._client.request(method, path, json=body, headers=headers)
            else:
                response = await self._client.request(
                    method, path, json=body, headers=headers, timeout=timeout
                )
        except httpx2.TransportError as error:
            raise NotReached(_unreached(self.url, str(error) or type(error).__name__)) from None
        if response.status_code in UNAVAILABLE:
            raise NotReached(_unavailable(self.url, response))
        if response.status_code >= 400:
            message, code = _error_of(response)
            if code in RETRY_CODES:
                raise NotReached(_unavailable(self.url, response))
            if code is None:
                raise Refused("unexpected", f"{message} (HTTP {response.status_code})")
            raise Refused(code, message)
        return response


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
    """The text of a call that did not reach the control plane."""
    return f"cannot reach the control plane at {url} ({why})"


def _unavailable(url: str, response: httpx2.Response) -> str:
    """The text of an answer that counts as "not reached". When the answer has a code,
    the text gives the code and the sentence. `not_serving` (another control plane
    took the database) is not an outage: the text says that the control plane does
    not serve its database now. Another control plane can take over at the same URL,
    so the supervisor tries again."""
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
        raise Refused("unexpected", "the control plane's answer is not JSON") from None


def _mapping(response: httpx2.Response) -> dict[str, Any]:
    data = _body(response)
    if not isinstance(data, dict):
        raise Refused("unexpected", "the control plane's answer is not a JSON object")
    return cast("dict[str, Any]", data)
