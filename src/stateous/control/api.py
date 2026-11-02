"""The control plane's HTTP API (see the docs, Reference > The HTTP API).

There are no keys and no tenants: a caller that reaches the port may call every
route. `stateous serve` listens on 127.0.0.1 by default. Every refusal is
`{"error": "<one plain sentence>", "code": "<code>"}`. The sentence is for
people and can change; the code is stable, and clients read it.
The engine is synchronous and does one transaction for each call, so each call
runs on a thread from the pool."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Collection
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from sqlalchemy.exc import OperationalError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from stateous.control import checks
from stateous.control.checks import MAX_TASK, Problem
from stateous.control.clock import LOST
from stateous.control.engine import Fired, HistoryEntry, Token
from stateous.control.errors import (
    ChildToken,
    ControlPlaneError,
    EventNotAllowed,
    InvalidArgument,
    InvalidContext,
    InvalidPayload,
    InvalidTokenId,
    TaskNotFound,
    TaskNotHeld,
    TokenNotActive,
    TokenNotFound,
)
from stateous.control.waiting import WaitingClaims
from stateous.model.wire import Outcome, TaskGrant, WireError

MAX_BODY = 1024 * 1024
"""The largest request body the API reads, in bytes: 1 MiB."""

MAX_DEPTH = 64
"""The deepest a request's JSON may nest. Contexts and payloads have a few levels."""

MAX_DIGITS = 18
"""The most digits a Content-Length or a `limit` may have before `int()` reads it."""

MAX_LIMIT = 1000
"""The most tokens one `GET /tokens` lists."""

MAX_KEY = 256
"""The longest idempotency key: the size of the ledger's `key` column."""

MAX_TOKEN = 128
"""The longest token id that a listing's `parent` names."""

STATUSES = ("active", "paused", "completed", "failed", "cancelled")

Handler = Callable[[Request, Exception], Awaitable[Response]]


class Served(Protocol):
    """What the API serves: a `ControlPlane`. The tests also give the internal engine."""

    @property
    def document(self) -> dict[str, Any]: ...

    def health(self) -> dict[str, Any]: ...

    @property
    def lost(self) -> bool: ...

    def on_task_ready(self, callback: Callable[[str], None]) -> Callable[[], None]: ...

    def start(self, token: str, context: dict[str, Any]) -> Token: ...

    def get(self, token: str) -> Token: ...

    def tokens(
        self, *, status: str | None = None, limit: int = 100, parent: str | None = None
    ) -> list[Token]: ...

    def history(self, token: str) -> list[HistoryEntry]: ...

    def fire(
        self,
        token: str,
        event: str,
        payload: dict[str, Any] | None,
        *,
        key: str | None = None,
    ) -> Fired: ...

    def pause(self, token: str) -> Token: ...

    def resume(self, token: str) -> Token: ...

    def cancel(self, token: str) -> Token: ...

    def claim(
        self, supervisor: str, states: Collection[str], *, request: str | None = None
    ) -> TaskGrant | None: ...

    def report(
        self,
        task: int,
        outcome: Outcome,
        *,
        supervisor: str | None = None,
        attempt: int | None = None,
    ) -> Fired: ...

    def heartbeat(self, supervisor: str, tasks: Collection[int]) -> list[int]: ...

    def note(self, task: int, text: str, *, attempt: int | None = None) -> None: ...

    def set_memory(
        self, task: int, key: str, value: Any, *, attempt: int | None = None
    ) -> None: ...


def create_app(engine: Served, *, waiting: WaitingClaims | None = None) -> Starlette:
    """The API over one control plane. It has no clock: `stateous serve` runs the clock.
    `waiting` holds the claims that wait for a task; `serve` closes it when it stops,
    so they answer at once."""
    document = engine.document
    health = engine.health()
    if waiting is None:
        waiting = WaitingClaims()
    engine.on_task_ready(waiting.ready)

    async def start(request: Request) -> Response:
        body = await body_of(request)
        token = token_id(body.get("token"))
        context = _object(body.get("context"), "context", "invalid_context") or {}
        existed = await run_in_threadpool(lambda: _exists(engine, token))
        started = await run_in_threadpool(lambda: engine.start(token, context))
        return JSONResponse(token_json(started), status_code=200 if existed else 201)

    async def list_tokens(request: Request) -> Response:
        status = request.query_params.get("status")
        if status is not None and status not in STATUSES:
            raise Problem(422, f"'status' is one of: {', '.join(STATUSES)}", "invalid_argument")
        limit = _limit(request.query_params.get("limit"))
        parent = _parent(request.query_params.get("parent"))
        found = await run_in_threadpool(
            lambda: engine.tokens(status=status, limit=limit, parent=parent)
        )
        return JSONResponse([token_json(token) for token in found])

    async def get_token(request: Request) -> Response:
        token: str = request.path_params["token"]
        return JSONResponse(token_json(await run_in_threadpool(lambda: engine.get(token))))

    async def history(request: Request) -> Response:
        token: str = request.path_params["token"]
        entries = await run_in_threadpool(lambda: engine.history(token))
        return JSONResponse([entry_json(entry) for entry in entries])

    async def fire(request: Request) -> Response:
        token: str = request.path_params["token"]
        event: str = request.path_params["event"]
        body = await body_of(request)
        payload = _object(body.get("payload"), "payload", "invalid_payload")
        key = request.headers.get("idempotency-key")
        if key is not None:
            _check_key(key)
        fired = await run_in_threadpool(lambda: engine.fire(token, event, payload, key=key))
        answer = token_json(fired.token)
        return JSONResponse({"duplicate": True, "token": answer} if fired.duplicate else answer)

    def steer(action: Callable[[str], Token]) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            token: str = request.path_params["token"]
            return JSONResponse(token_json(await run_in_threadpool(lambda: action(token))))

        return endpoint

    async def workflow(request: Request) -> Response:
        return JSONResponse(document)

    async def health_check(request: Request) -> Response:
        return JSONResponse(health)

    async def claim(request: Request) -> Response:
        body = await body_of(request)
        supervisor = checks.supervisor_name(body.get("supervisor"))
        states = checks.state_names(body.get("states"), "states")
        request_id = checks.request_id(body.get("request"))
        wait = checks.claim_wait(body.get("wait"))

        def claimed() -> TaskGrant | None:
            return engine.claim(supervisor, states, request=request_id)

        if wait == 0:
            grant = await run_in_threadpool(claimed)
        else:
            with waiting.waiting(states) as ready:
                grant = await run_in_threadpool(claimed)
                if grant is None and await _woken(ready, request, wait):
                    grant = await run_in_threadpool(claimed)
        if grant is None:
            return Response(status_code=204)
        return JSONResponse(grant.to_json())

    async def report(request: Request) -> Response:
        task = _task(request)
        body = await body_of(request)
        named = body.pop("supervisor", None)
        supervisor = None if named is None else checks.supervisor_name(named)
        outcome = Outcome.from_json(body)
        fired = await run_in_threadpool(lambda: engine.report(task, outcome, supervisor=supervisor))
        return JSONResponse(token_json(fired.token))

    async def heartbeat(request: Request) -> Response:
        body = await body_of(request)
        supervisor = checks.supervisor_name(body.get("supervisor"))
        tasks = checks.task_ids(body.get("tasks"))
        lost = await run_in_threadpool(lambda: engine.heartbeat(supervisor, tasks))
        return JSONResponse({"lost": lost})

    async def note(request: Request) -> Response:
        task = _task(request)
        body = await body_of(request)
        text, attempt = checks.note_text(body.get("text")), checks.attempt(body.get("attempt"))
        await run_in_threadpool(lambda: engine.note(task, text, attempt=attempt))
        return Response(status_code=204)

    async def memory(request: Request) -> Response:
        task = _task(request)
        body = await body_of(request)
        key = checks.name(body.get("key"), "key", code="invalid_payload")
        if "value" not in body:
            raise Problem(422, "memory needs a 'value'; null deletes the key", "invalid_body")
        value, attempt = body["value"], checks.attempt(body.get("attempt"))
        await run_in_threadpool(lambda: engine.set_memory(task, key, value, attempt=attempt))
        return Response(status_code=204)

    routes = [
        Route("/tokens", start, methods=["POST"]),
        Route("/tokens", list_tokens, methods=["GET"]),
        Route("/tokens/{token}", get_token, methods=["GET"]),
        Route("/tokens/{token}/history", history, methods=["GET"]),
        Route("/tokens/{token}/events/{event}", fire, methods=["POST"]),
        Route("/tokens/{token}/pause", steer(engine.pause), methods=["POST"]),
        Route("/tokens/{token}/resume", steer(engine.resume), methods=["POST"]),
        Route("/tokens/{token}/cancel", steer(engine.cancel), methods=["POST"]),
        Route("/workflow", workflow, methods=["GET"]),
        Route("/health", health_check, methods=["GET"]),
        Route("/tasks/claim", claim, methods=["POST"]),
        Route("/tasks/heartbeat", heartbeat, methods=["POST"]),
        Route("/tasks/{task:int}/report", report, methods=["POST"]),
        Route("/tasks/{task:int}/note", note, methods=["POST"]),
        Route("/tasks/{task:int}/memory", memory, methods=["PUT"]),
    ]
    return Starlette(
        routes=routes,
        middleware=[
            Middleware(NotServing, served=engine),
            Middleware(BodyLimit, limit=MAX_BODY),
        ],
        exception_handlers=dict(_HANDLERS),
    )


# ── what the API sends ──────────────────────────────────────────────────


async def _woken(ready: asyncio.Event, request: Request, wait: float) -> bool:
    """Wait up to `wait` seconds for `ready`. False when the client went away first:
    no one reads the answer, so the claim must not take a task."""
    readied = asyncio.ensure_future(ready.wait())
    gone = asyncio.ensure_future(_disconnected(request))
    try:
        await asyncio.wait({readied, gone}, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
    finally:
        readied.cancel()
        gone.cancel()
    return not (gone.done() and not gone.cancelled())


async def _disconnected(request: Request) -> None:
    """Return when the client goes away. The body is read already, so the next message
    is the disconnect."""
    while (await request.receive())["type"] != "http.disconnect":
        pass


def token_json(token: Token) -> dict[str, Any]:
    """A token as the API sends it. A child has `parent`; a top token has no such key."""
    out: dict[str, Any] = {
        "token": token.token,
        "state": token.state,
        "status": token.status,
        "visit": token.visit,
        "context": token.context,
        "entered_at": iso(token.entered_at),
    }
    if token.parent is not None:
        out["parent"] = token.parent
    return out


def entry_json(entry: HistoryEntry) -> dict[str, Any]:
    return {"seq": entry.seq, "at": iso(entry.at), "kind": entry.kind, "data": entry.data}


def iso(at: datetime) -> str:
    """`at` in ISO 8601, UTC, with `Z`. A naive time is UTC."""
    aware = at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


# ── what the API accepts ────────────────────────────────────────────────


def token_id(value: Any) -> str:
    """`value` as a new token's id; 422 if it is not a string. The engine checks the
    id's form and the case-folded clash, and raises `InvalidTokenId` (422)."""
    if not isinstance(value, str):
        raise Problem(422, "'token' is the token's id: a string", "invalid_token_id")
    return value


def _exists(engine: Served, token: str) -> bool:
    try:
        engine.get(token)
    except TokenNotFound:
        return False
    return True


def _object(value: Any, what: str, code: str) -> dict[str, Any] | None:
    """A JSON object from a body, or None when it is absent or null. Else 422 with
    `code`."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise Problem(422, f"'{what}' must be a JSON object, or null", code)
    return cast("dict[str, Any]", value)


def _limit(value: str | None) -> int:
    if value is None:
        return 100
    wrong = f"'limit' is a whole number from 1 to {MAX_LIMIT}"
    if not value.isdecimal() or len(value) > MAX_DIGITS:
        raise Problem(422, wrong, "invalid_argument")
    limit = int(value)
    if not 1 <= limit <= MAX_LIMIT:
        raise Problem(422, wrong, "invalid_argument")
    return limit


def _parent(value: str | None) -> str | None:
    """The `parent` of a listing: a token id; None when the query names none."""
    if value is not None and not 1 <= len(value) <= MAX_TOKEN:
        raise Problem(
            422, f"'parent' is a token id of 1 to {MAX_TOKEN} characters", "invalid_argument"
        )
    return value


def _check_key(key: str) -> None:
    if not 1 <= len(key) <= MAX_KEY:
        raise Problem(422, f"an idempotency key has 1 to {MAX_KEY} characters", "invalid_payload")
    if any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise Problem(422, "an idempotency key has no control characters", "invalid_payload")


def _task(request: Request) -> int:
    """The task id in the path. An id that no database integer holds is not there."""
    task: int = request.path_params["task"]
    if task > MAX_TASK:
        raise Problem(404, f"there is no task {task}", "task_not_found")
    return task


async def body_of(request: Request) -> dict[str, Any]:
    """A request's body: a JSON object, or {} when the body is empty."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body: Any = read_json(raw)
    except ValueError as error:
        raise Problem(400, f"the body is not JSON: {error}") from None
    if not isinstance(body, dict):
        raise Problem(422, "the body must be a JSON object", "invalid_body")
    return cast("dict[str, Any]", body)


def read_json(raw: bytes) -> Any:
    """`raw` as strict JSON. NaN, Infinity and nesting past MAX_DEPTH are refused.
    `ValueError`, with a plain message, for anything that is not JSON."""

    def constant(name: str) -> Any:
        raise ValueError(f"{name} is not a JSON value; send a number, or null")

    too_deep = f"it is nested more than {MAX_DEPTH} levels deep"
    try:
        value = json.loads(raw, parse_constant=constant)
    except RecursionError:
        raise ValueError(too_deep) from None
    if _depth_over(value, MAX_DEPTH):
        raise ValueError(too_deep)
    return value


def _depth_over(value: Any, limit: int) -> bool:
    """Whether `value` nests more than `limit` objects or lists deep. No recursion."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children: list[Any] = list(cast("dict[str, Any]", item).values())
        elif isinstance(item, list):
            children = cast("list[Any]", item)
        else:
            continue
        if depth + 1 > limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


class NotServing:
    """ASGI middleware. After another control plane took the database, each request
    gets 503 with the code `not_serving`: a supervisor tries again, and can reach the
    control plane that serves now at the same URL."""

    def __init__(self, app: ASGIApp, served: Served) -> None:
        self.app = app
        self.served = served

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.served.lost:
            await refusal(503, LOST, "not_serving")(scope, receive, send)
            return
        await self.app(scope, receive, send)


class BodyLimit:
    """ASGI middleware. It refuses a request body larger than `limit` bytes with 413,
    before a route reads it. It reads the Content-Length when that frames the body,
    and it always counts the bytes as they arrive, so it also stops a chunked body.
    Beside Transfer-Encoding, it ignores a Content-Length (RFC 9112, section 6.3)."""

    def __init__(self, app: ASGIApp, limit: int = MAX_BODY) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in cast("list[tuple[bytes, bytes]]", scope["headers"])}
        declared = headers.get(b"content-length", b"").strip()
        chunked = b"transfer-encoding" in headers
        too_long = len(declared) > MAX_DIGITS
        if not chunked and declared.isdigit() and (too_long or int(declared) > self.limit):
            await _send_json(send, 413, self._too_large())
            return
        arrived: list[Message] = []
        seen = 0
        while True:
            message = await receive()
            arrived.append(message)
            if message["type"] != "http.request":
                break  # the client went away: the route sees that
            seen += len(cast("bytes", message.get("body", b"")))
            if seen > self.limit:
                await _send_json(send, 413, self._too_large())
                return
            if not message.get("more_body", False):
                break

        async def replayed() -> Message:
            return arrived.pop(0) if arrived else await receive()

        await self.app(scope, replayed, send)

    def _too_large(self) -> dict[str, str]:
        """The refusal of a body that is too large. The table of codes has no row for
        it: `bad_request` is the nearest, with the status of the problem (413)."""
        error = f"the body is larger than {_size(self.limit)}, the most this API reads"
        return {"error": error, "code": "bad_request"}


def _size(limit: int) -> str:
    mib = 1024 * 1024
    return f"{limit // mib} MiB" if limit % mib == 0 else f"{limit} bytes"


async def _send_json(send: Send, status: int, content: dict[str, Any]) -> None:
    body = json.dumps(content).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


# ── refusals ────────────────────────────────────────────────────────────


def refusal(status: int, message: str, code: str) -> JSONResponse:
    """A refusal: `{"error": message, "code": code}` with `status`."""
    return JSONResponse({"error": message, "code": code}, status_code=status)


async def _problem(request: Request, error: Exception) -> Response:
    if isinstance(error, Problem):
        return refusal(error.status, str(error), error.code)
    # `_problem` is registered only for Problem.
    return refusal(400, str(error), "bad_request")  # pragma: no cover


async def _http_error(request: Request, error: Exception) -> Response:
    """Starlette's own refusals (no such path, a wrong method) as JSON. Another
    status is `bad_request` below 500, else `unexpected`."""
    status = error.status_code if isinstance(error, HTTPException) else 500
    if status == 404:
        return refusal(404, f"this API has no {request.url.path}", "not_found")
    if status == 405:
        message = f"{request.url.path} does not take {request.method}"
        return refusal(405, message, "method_not_allowed")
    detail = error.detail if isinstance(error, HTTPException) else "the request failed"
    return refusal(status, str(detail), "unexpected" if status >= 500 else "bad_request")


def _answer(status: int, code: str | None = None) -> Handler:
    """An exception handler that answers `status` with the error's one sentence. The
    code is `code`; without it, the code that the engine error declares."""

    async def handle(request: Request, error: Exception) -> Response:
        message = str(error.args[0]) if error.args else type(error).__name__
        if code is not None:
            named = code
        elif isinstance(error, ControlPlaneError):
            named = error.code
        else:  # pragma: no cover - each exception registered without a code is a ControlPlaneError
            named = "unexpected"
        return refusal(status, message, named)

    return handle


async def _database_busy(request: Request, error: Exception) -> Response:
    """The database could not finish the transaction. One sentence; no detail."""
    return refusal(503, "the database cannot take this request now; try again", "database_busy")


async def _unexpected(request: Request, error: Exception) -> Response:
    """Anything no handler above names: one sentence, never a traceback in the body."""
    return refusal(500, "the request failed unexpectedly", "unexpected")


_HANDLERS: dict[Any, Handler] = {
    Problem: _problem,
    HTTPException: _http_error,
    TokenNotFound: _answer(404),
    InvalidArgument: _answer(422),
    InvalidContext: _answer(422),
    InvalidPayload: _answer(422),
    InvalidTokenId: _answer(422),
    EventNotAllowed: _answer(409),
    TokenNotActive: _answer(409),
    ChildToken: _answer(409),
    TaskNotFound: _answer(404),
    TaskNotHeld: _answer(409),
    WireError: _answer(422, "invalid_body"),
    OperationalError: _database_busy,
    Exception: _unexpected,
}
