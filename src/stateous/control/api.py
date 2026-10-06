"""The control plane's HTTP API (docs/design.md, section 5.4).

There are no keys and no tenants: a caller that reaches the port may call every
route. `stateous serve` listens on 127.0.0.1 by default. Every refusal is
`{"error": "<one plain sentence>"}`. The engine is synchronous and does one
transaction for each call, so each call runs on a thread from the pool."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy.exc import OperationalError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from stateous.control.engine import (
    Engine,
    Entry,
    EventNotAllowed,
    InvalidArgument,
    InvalidContext,
    InvalidPayload,
    InvalidTokenId,
    TaskNotFound,
    TaskNotHeld,
    Token,
    TokenNotActive,
    TokenNotFound,
)
from stateous.model.wire import Outcome, WireError

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

MAX_NAME = 128
"""The longest state name in a claim, or memory key."""

MAX_ITEMS = 1000
"""The most state names in a claim, and the most task ids in a heartbeat."""

MAX_SUPERVISOR = 256
"""The longest supervisor name: the engine's own limit."""

MAX_NOTE = 500
"""The longest note, after the spaces at its two ends are removed."""

MAX_TASK = 2**63 - 1
"""The largest task id that a database integer holds."""

STATUSES = ("active", "paused", "completed", "failed", "cancelled")

Handler = Callable[[Request, Exception], Awaitable[Response]]


class Problem(Exception):
    """A request that the API refuses, with the status and one plain sentence."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def create_app(engine: Engine) -> Starlette:
    """The API over one engine. It has no clock: `stateous serve` runs the clock."""
    document = engine.graph.document
    health = {"workflow": engine.graph.name, "checksum": engine.graph.checksum}

    async def start(request: Request) -> Response:
        body = await body_of(request)
        token = token_id(body.get("token"))
        context = _object(body.get("context"), "context") or {}
        existed = await run_in_threadpool(lambda: _exists(engine, token))
        started = await run_in_threadpool(lambda: engine.start(token, context))
        return JSONResponse(token_json(started), status_code=200 if existed else 201)

    async def list_tokens(request: Request) -> Response:
        status = request.query_params.get("status")
        if status is not None and status not in STATUSES:
            raise Problem(422, f"'status' is one of: {', '.join(STATUSES)}")
        limit = _limit(request.query_params.get("limit"))
        found = await run_in_threadpool(lambda: engine.tokens(status=status, limit=limit))
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
        payload = _object((await body_of(request)).get("payload"), "payload")
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
        supervisor = _name(body.get("supervisor"), "supervisor", MAX_SUPERVISOR)
        states = _names(body.get("states"), "states")
        grant = await run_in_threadpool(lambda: engine.claim(supervisor, states))
        if grant is None:
            return Response(status_code=204)
        return JSONResponse(grant.to_json())

    async def report(request: Request) -> Response:
        task = _task(request)
        body = await body_of(request)
        named = body.pop("supervisor", None)
        supervisor = None if named is None else _name(named, "supervisor", MAX_SUPERVISOR)
        outcome = Outcome.from_json(body)
        fired = await run_in_threadpool(lambda: engine.report(task, outcome, supervisor=supervisor))
        return JSONResponse(token_json(fired.token))

    async def heartbeat(request: Request) -> Response:
        body = await body_of(request)
        supervisor = _name(body.get("supervisor"), "supervisor", MAX_SUPERVISOR)
        tasks = _task_ids(body.get("tasks"))
        lost = await run_in_threadpool(lambda: engine.heartbeat(supervisor, tasks))
        return JSONResponse({"lost": lost})

    async def note(request: Request) -> Response:
        task = _task(request)
        text = _note((await body_of(request)).get("text"))
        await run_in_threadpool(lambda: engine.note(task, text))
        return Response(status_code=204)

    async def memory(request: Request) -> Response:
        task = _task(request)
        body = await body_of(request)
        key = _name(body.get("key"), "key")
        if "value" not in body:
            raise Problem(422, "memory needs a 'value'; null deletes the key")
        value = body["value"]
        await run_in_threadpool(lambda: engine.set_memory(task, key, value))
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
        middleware=[Middleware(BodyLimit, limit=MAX_BODY)],
        exception_handlers=dict(_HANDLERS),
    )


# ── what the API sends ──────────────────────────────────────────────────


def token_json(token: Token) -> dict[str, Any]:
    return {
        "token": token.token,
        "state": token.state,
        "status": token.status,
        "visit": token.visit,
        "context": token.context,
        "entered_at": iso(token.entered_at),
    }


def entry_json(entry: Entry) -> dict[str, Any]:
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
        raise Problem(422, "'token' is the token's id: a string")
    return value


def _exists(engine: Engine, token: str) -> bool:
    try:
        engine.get(token)
    except TokenNotFound:
        return False
    return True


def _object(value: Any, what: str) -> dict[str, Any] | None:
    """A JSON object from a body, or None when it is absent or null."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise Problem(422, f"'{what}' must be a JSON object, or null")
    return cast("dict[str, Any]", value)


def _limit(value: str | None) -> int:
    if value is None:
        return 100
    wrong = f"'limit' is a whole number from 1 to {MAX_LIMIT}"
    if not value.isdecimal() or len(value) > MAX_DIGITS:
        raise Problem(422, wrong)
    limit = int(value)
    if not 1 <= limit <= MAX_LIMIT:
        raise Problem(422, wrong)
    return limit


def _check_key(key: str) -> None:
    if not 1 <= len(key) <= MAX_KEY:
        raise Problem(422, f"an idempotency key has 1 to {MAX_KEY} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise Problem(422, "an idempotency key has no control characters")


def _task(request: Request) -> int:
    """The task id in the path. An id that no database integer holds is not there."""
    task: int = request.path_params["task"]
    if task > MAX_TASK:
        raise Problem(404, f"there is no task {task}")
    return task


def _name(value: Any, what: str, limit: int = MAX_NAME) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise Problem(422, f"'{what}' is a string of 1 to {limit} characters")
    return value


def _names(value: Any, what: str) -> list[str]:
    items = cast("list[Any]", value) if isinstance(value, list) else None
    if items is None or not all(isinstance(i, str) and 1 <= len(i) <= MAX_NAME for i in items):
        raise Problem(422, f"'{what}' is a list of state names")
    if len(items) > MAX_ITEMS:
        raise Problem(422, f"'{what}' has at most {MAX_ITEMS} state names")
    return cast("list[str]", items)


def _task_ids(value: Any) -> list[int]:
    items = cast("list[Any]", value) if isinstance(value, list) else None
    if items is None or not all(
        isinstance(i, int) and not isinstance(i, bool) and 0 < i <= MAX_TASK for i in items
    ):
        raise Problem(422, "'tasks' is a list of task ids")
    if len(items) > MAX_ITEMS:
        raise Problem(422, f"'tasks' has at most {MAX_ITEMS} task ids")
    return cast("list[int]", items)


def _note(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not 1 <= len(text) <= MAX_NOTE:
        raise Problem(422, f"a note needs a 'text' of 1 to {MAX_NOTE} characters")
    return text


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
        raise Problem(422, "the body must be a JSON object")
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
            await _send_json(send, 413, {"error": self._too_large()})
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
                await _send_json(send, 413, {"error": self._too_large()})
                return
            if not message.get("more_body", False):
                break

        async def replayed() -> Message:
            return arrived.pop(0) if arrived else await receive()

        await self.app(scope, replayed, send)

    def _too_large(self) -> str:
        return f"the body is larger than {_size(self.limit)}, the most this API reads"


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


def refusal(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _problem(request: Request, error: Exception) -> Response:
    return refusal(error.status if isinstance(error, Problem) else 400, str(error))


async def _http_error(request: Request, error: Exception) -> Response:
    """Starlette's own refusals (no such path, a wrong method) as JSON."""
    status = error.status_code if isinstance(error, HTTPException) else 500
    if status == 404:
        return refusal(404, f"this API has no {request.url.path}")
    if status == 405:
        return refusal(405, f"{request.url.path} does not take {request.method}")
    detail = error.detail if isinstance(error, HTTPException) else "the request failed"
    return refusal(status, str(detail))


def _answer(status: int) -> Handler:
    """An exception handler that answers `status` with the error's one sentence."""

    async def handle(request: Request, error: Exception) -> Response:
        message = str(error.args[0]) if error.args else type(error).__name__
        return refusal(status, message)

    return handle


async def _database_busy(request: Request, error: Exception) -> Response:
    """The database could not finish the transaction. One sentence; no detail."""
    return refusal(503, "the database cannot take this request now; try again")


async def _unexpected(request: Request, error: Exception) -> Response:
    """Anything no handler above names: one sentence, never a traceback in the body."""
    return refusal(500, "the request failed unexpectedly")


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
    TaskNotFound: _answer(404),
    TaskNotHeld: _answer(409),
    WireError: _answer(422),
    OperationalError: _database_busy,
    Exception: _unexpected,
}
