"""The client: a program outside starts tokens, fires events and reads tokens and
their history over the control plane's HTTP API (docs/design.md, section 5.3).

    from stateous import Client

    with Client("http://127.0.0.1:8080") as client:
        client.start("TICKET-1", {"repo": "acme/api"})
        client.fire("TICKET-1", "done", key="gh-delivery-81231")

Every failure is a `ClientError` with one plain sentence: `Unreachable` (no
answer), or `Refused` with the HTTP status (`NotFound` for 404). It imports nothing
of the control plane: it speaks only HTTP."""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import quote

import httpx2


class ClientError(Exception):
    """Something the control plane could not do, or could not be asked."""


class Unreachable(ClientError):
    """The control plane did not answer."""


class Refused(ClientError):
    """The control plane answered no. `status` is the HTTP status, and `message` is
    the reason it gave."""

    def __init__(self, status: int, message: str, *, url: str) -> None:
        super().__init__(f"the control plane at {url} refused ({status}): {message}")
        self.status = status
        self.message = message


class NotFound(Refused):
    """The control plane answered 404: no such token, task or path."""


class Client:
    """The control plane's HTTP API at one URL. Synchronous."""

    def __init__(self, url: str, *, timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        try:
            self._http = httpx2.Client(base_url=self.url, timeout=timeout)
        except httpx2.InvalidURL as error:
            raise ClientError(_not_a_url(self.url, error)) from None

    def start(self, token: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start a token. A token that exists is returned as it is."""
        return self._object("POST", "/tokens", {"token": token, "context": context or {}})

    def get(self, token: str) -> dict[str, Any]:
        return self._object("GET", f"/tokens/{_segment(token)}")

    def tokens(
        self, *, status: str | None = None, parent: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """The tokens, the most recently moved first. With `parent`: its children only.
        With `limit` (1 to 1000): at most that many; else at most 100."""
        params: dict[str, str] = {}
        if status is not None:
            params["status"] = status
        if parent is not None:
            params["parent"] = parent
        if limit is not None:
            params["limit"] = str(limit)
        return self._list("GET", "/tokens", params=params)

    def history(self, token: str) -> list[dict[str, Any]]:
        return self._list("GET", f"/tokens/{_segment(token)}/history")

    def fire(
        self,
        token: str,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        key: str | None = None,
    ) -> dict[str, Any]:
        """Fire `event` on a token. The answer is the token; or, when `key` was used
        before, `{"duplicate": True, "token": ...}`."""
        headers = {} if key is None else {"Idempotency-Key": key}
        body = {} if payload is None else {"payload": payload}
        path = f"/tokens/{_segment(token)}/events/{_segment(event)}"
        return self._object("POST", path, body, headers=headers)

    def pause(self, token: str) -> dict[str, Any]:
        return self._object("POST", f"/tokens/{_segment(token)}/pause", {})

    def resume(self, token: str) -> dict[str, Any]:
        return self._object("POST", f"/tokens/{_segment(token)}/resume", {})

    def cancel(self, token: str) -> dict[str, Any]:
        return self._object("POST", f"/tokens/{_segment(token)}/cancel", {})

    def report(
        self,
        task: int,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        attempt: int | None = None,
    ) -> dict[str, Any]:
        """The outcome of a task that waits for the outside (its routine returned None).
        With `attempt`: only for that attempt of the task. The control plane refuses
        it (409) when the task is on another attempt."""
        body: dict[str, Any] = {"kind": "event", "event": event}
        if payload is not None:
            body["payload"] = payload
        if attempt is not None:
            body["attempt"] = attempt
        return self._object("POST", f"/tasks/{task}/report", body)

    def workflow(self) -> dict[str, Any]:
        """The graph document that the control plane serves."""
        return self._object("GET", "/workflow")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _object(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        answer = self._call(method, path, body, headers=headers)
        if not isinstance(answer, dict):
            raise ClientError(self._not_ours())
        return cast("dict[str, Any]", answer)

    def _list(
        self, method: str, path: str, *, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        answer = self._call(method, path, params=params)
        if not isinstance(answer, list):
            raise ClientError(self._not_ours())
        return cast("list[dict[str, Any]]", answer)

    def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        try:
            response = self._http.request(method, path, json=body, headers=headers, params=params)
        except httpx2.InvalidURL as error:
            raise ClientError(_not_a_url(self.url + path, error)) from None
        except httpx2.TransportError as error:
            raise Unreachable(f"cannot reach the control plane at {self.url}: {error}") from None
        if response.status_code >= 400:
            raise self._refused(response)
        try:
            return response.json()
        except ValueError:
            raise ClientError(
                f"the control plane at {self.url} answered {response.status_code} with "
                "something that is not JSON; check the URL"
            ) from None

    def _refused(self, response: httpx2.Response) -> Refused:
        try:
            answer: Any = response.json()
        except ValueError:
            answer = None
        error = cast("dict[str, Any]", answer).get("error") if isinstance(answer, dict) else None
        if isinstance(error, str):
            message = error
        else:
            message = response.text[:200].strip() or response.reason_phrase
        kind = NotFound if response.status_code == 404 else Refused
        return kind(response.status_code, message, url=self.url)

    def _not_ours(self) -> str:
        return f"the control plane at {self.url} sent an answer of the wrong shape; check the URL"


def _not_a_url(url: str, error: Exception) -> str:
    """One sentence for a URL that httpx2 cannot build. A long URL is cut short."""
    shown = url if len(url) <= 100 else f"{url[:100]}..."
    return f"{shown} is not a URL: {' '.join(str(error).split())}"


def _segment(name: str) -> str:
    """`name` with each reserved character escaped, a `/` too (as %2F). The server
    decodes %2F back to `/` before it matches a route, so a name with a `/` does not
    reach the token's routes: the answer is 404."""
    return quote(name, safe="")
