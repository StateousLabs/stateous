"""The client: a program outside starts tokens, fires events and reads tokens and
their history over the control plane's HTTP API (see the guide, "The HTTP API").

    from stateous import Client

    with Client("http://127.0.0.1:8080") as client:
        client.start("TICKET-1", {"repo": "acme/api"})
        client.fire("TICKET-1", "done", key="gh-delivery-81231")

Every failure is a `ClientError` with one plain sentence: `Unreachable` (no
answer), or `Refused` with the HTTP status and the refusal's code (`NotFound` for
404). Read the code, never the sentence: the sentence can change. It imports nothing
of the control plane: it speaks only HTTP."""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import quote

import httpx2

from stateous.model.host import host_problem


class ClientError(Exception):
    """The base of every error of the client. The control plane could not do the
    request, or the client could not send it. The message is one plain sentence."""


class Unreachable(ClientError):
    """The control plane did not answer: the network failed, or no server listens
    at the URL."""


class Refused(ClientError):
    """The control plane refused the request.

    Use `code` in a program, for example `attempt_over`. The codes are in the docs,
    Reference > The HTTP API. `message` is for people, and it can change.

    Attributes:
        status: The HTTP status, for example 409.
        message: The reason that the control plane gave, as one sentence.
        code: The stable code of the refusal. None when the answer has no code,
            for example an answer from a proxy.
    """

    def __init__(self, status: int, message: str, *, url: str, code: str | None = None) -> None:
        """The client makes it from the control plane's answer."""
        super().__init__(f"the control plane at {url} refused ({status}): {message}")
        self.status = status
        self.message = message
        self.code = code


class NotFound(Refused):
    """The control plane answered 404: no token, task or path has this name."""


class Client:
    """The control plane's HTTP API at one URL, for a program. Synchronous.

    Each method sends one request, and returns the JSON answer. A failure raises
    a `ClientError`. Use the client in a `with` block, or call `close()` at the
    end.

    ```python
    from stateous import Client

    with Client("http://127.0.0.1:8080") as client:
        client.start("TICKET-1", {"repo": "acme/api"})
        client.fire("TICKET-1", "done", key="gh-delivery-81231")
    ```
    """

    def __init__(self, url: str, *, timeout: float = 30.0) -> None:
        """Make a client for one control plane.

        Args:
            url: The control plane's URL, for example `http://127.0.0.1:8080`.
            timeout: How long to wait for each answer, in seconds.

        Raises:
            ClientError: `url` is not a valid URL.
        """
        self.url = url.rstrip("/")
        try:
            http = httpx2.Client(base_url=self.url, timeout=timeout)
        except httpx2.InvalidURL as error:
            raise ClientError(_not_a_url(self.url, error)) from None
        problem = host_problem(http.base_url.host)
        if problem is not None:
            raise ClientError(_bad_host(self.url, problem))
        self._http = http

    def start(self, token: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start a token. If the token exists, it is returned unchanged.

        Args:
            token: The token's id, for example `TICKET-1`.
            context: The token's context. None: `{}`.

        Returns:
            The token.
        """
        return self._object("POST", "/tokens", {"token": token, "context": context or {}})

    def get(self, token: str) -> dict[str, Any]:
        """Read one token.

        Args:
            token: The token's id.

        Returns:
            The token: its state, status, visit, context and the time it entered
                the state.

        Raises:
            NotFound: No token has this id.
        """
        return self._object("GET", f"/tokens/{_segment(token)}")

    def tokens(
        self, *, status: str | None = None, parent: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """List the tokens, the most recently moved first.

        Args:
            status: Only the tokens with this status, for example `"active"`.
            parent: Only the children of this token.
            limit: The most tokens to return: 1 to 1000. None: 100.

        Returns:
            The tokens.
        """
        params: dict[str, str] = {}
        if status is not None:
            params["status"] = status
        if parent is not None:
            params["parent"] = parent
        if limit is not None:
            params["limit"] = str(limit)
        return self._list("GET", "/tokens", params=params)

    def history(self, token: str) -> list[dict[str, Any]]:
        """Read the history of a token, in order.

        Args:
            token: The token's id.

        Returns:
            The entries: each has `seq`, `at`, `kind` and `data`.

        Raises:
            NotFound: No token has this id.
        """
        return self._list("GET", f"/tokens/{_segment(token)}/history")

    def fire(
        self,
        token: str,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        key: str | None = None,
    ) -> dict[str, Any]:
        """Fire an event on a token.

        Args:
            token: The token's id.
            event: The event's name.
            payload: The event's payload. None: no payload.
            key: An idempotency key, from 1 to 256 characters. The same key fires
                one time.

        Returns:
            The token after the event. When `key` was used before, the answer is
                `{"duplicate": True, "token": ...}`, and nothing changes.

        Raises:
            Refused: The event cannot move the token, or the payload is not valid.
        """
        headers = {} if key is None else {"Idempotency-Key": key}
        body = {} if payload is None else {"payload": payload}
        path = f"/tokens/{_segment(token)}/events/{_segment(event)}"
        return self._object("POST", path, body, headers=headers)

    def pause(self, token: str) -> dict[str, Any]:
        """Pause an active token. Its events are held, its tasks wait, and its
        timers stand still.

        Args:
            token: The token's id. A child token cannot be paused: pause its top
                token.

        Returns:
            The token.
        """
        return self._object("POST", f"/tokens/{_segment(token)}/pause", {})

    def resume(self, token: str) -> dict[str, Any]:
        """Resume a paused token. The held events apply in the order that they
        arrived.

        Args:
            token: The token's id.

        Returns:
            The token.
        """
        return self._object("POST", f"/tokens/{_segment(token)}/resume", {})

    def cancel(self, token: str) -> dict[str, Any]:
        """Cancel an active or a paused token for good.

        Args:
            token: The token's id.

        Returns:
            The token.
        """
        return self._object("POST", f"/tokens/{_segment(token)}/cancel", {})

    def report(
        self,
        task: int,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        attempt: int | None = None,
    ) -> dict[str, Any]:
        """Report the outcome of a task that waits. Its routine returned None, and
        the work went on outside.

        Args:
            task: The task's id: `ctx.task` in the routine.
            event: The event's name.
            payload: The event's payload. None: no payload.
            attempt: The attempt that the report is for. The control plane refuses
                the report (409) when the task is on a different attempt.

        Returns:
            The token after the event.
        """
        body: dict[str, Any] = {"kind": "event", "event": event}
        if payload is not None:
            body["payload"] = payload
        if attempt is not None:
            body["attempt"] = attempt
        return self._object("POST", f"/tasks/{task}/report", body)

    def workflow(self) -> dict[str, Any]:
        """Read the graph file that the control plane serves.

        Returns:
            The graph file, as JSON.
        """
        return self._object("GET", "/workflow")

    def close(self) -> None:
        """Close the connection. A `with` block calls it at the end."""
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
        body = cast("dict[str, Any]", answer) if isinstance(answer, dict) else {}
        error, code = body.get("error"), body.get("code")
        if isinstance(error, str):
            message = error
        else:
            message = response.text[:200].strip() or response.reason_phrase
        kind = NotFound if response.status_code == 404 else Refused
        named = code if isinstance(code, str) else None
        return kind(response.status_code, message, url=self.url, code=named)

    def _not_ours(self) -> str:
        return f"the control plane at {self.url} sent an answer of the wrong shape; check the URL"


def _not_a_url(url: str, error: Exception) -> str:
    """One sentence for a URL that httpx2 cannot build. A long URL is cut short."""
    shown = url if len(url) <= 100 else f"{url[:100]}..."
    return f"{shown} is not a URL: {' '.join(str(error).split())}"


def _bad_host(url: str, problem: str) -> str:
    """One sentence for a URL whose host `host_problem` refuses. A long URL is cut
    short."""
    shown = url if len(url) <= 100 else f"{url[:100]}..."
    return f"{shown} has a host that is not valid: {problem}"


def _segment(name: str) -> str:
    """`name` with each reserved character escaped, a `/` too (as %2F). The server
    decodes %2F back to `/` before it matches a route, so a name with a `/` does not
    reach the token's routes: the answer is 404."""
    return quote(name, safe="")
