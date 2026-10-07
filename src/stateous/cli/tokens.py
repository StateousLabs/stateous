"""`stateous token ...`: start, fire, show, pause, resume and cancel tokens over the
control plane's HTTP API (docs/design.md, section 5.5). The output is ASCII."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, cast

import typer

from stateous.cli.errors import CommandError, reported
from stateous.client import Client
from stateous.compiler.plot import mermaid

DEFAULT_URL = "http://127.0.0.1:8080"

token_app = typer.Typer(
    help="Act on tokens: start, fire, show, pause, resume, cancel.", no_args_is_help=True
)

Url = Annotated[str, typer.Option("--url", envvar="STATEOUS_URL", help="The control plane's URL.")]
TokenId = Annotated[
    str, typer.Argument(metavar="ID", help="The token's id, e.g. TICKET-1.", show_default=False)
]


@token_app.command("start")
@reported
def start(
    token: TokenId,
    context: Annotated[
        str | None,
        typer.Argument(metavar="CONTEXT_JSON", help="The token's context: a JSON object."),
    ] = None,
    url: Url = DEFAULT_URL,
) -> None:
    """Start a token. Starting one that exists shows it."""
    given = {} if context is None else json_object(context, "CONTEXT_JSON")
    with Client(url) as client:
        typer.echo(token_line(client.start(token, given)))


@token_app.command("fire")
@reported
def fire(
    token: TokenId,
    event: Annotated[str, typer.Argument(metavar="EVENT", help="The event's name.")],
    payload: Annotated[
        str | None,
        typer.Argument(metavar="PAYLOAD_JSON", help="The event's payload: a JSON object."),
    ] = None,
    key: Annotated[
        str | None, typer.Option("--key", help="An idempotency key: the same key fires once.")
    ] = None,
    url: Url = DEFAULT_URL,
) -> None:
    """Fire an event on a token."""
    given = None if payload is None else json_object(payload, "PAYLOAD_JSON")
    with Client(url) as client:
        answer = client.fire(token, event, given, key=key)
    if answer.get("duplicate") is True:
        typer.echo("duplicate: " + token_line(cast("dict[str, Any]", answer["token"])))
    else:
        typer.echo(token_line(answer))


@token_app.command("show")
@reported
def show(
    token: Annotated[
        str | None,
        typer.Argument(metavar="ID", help="A token; leave it out to list the tokens."),
    ] = None,
    graph: Annotated[
        bool, typer.Option("--graph", help="Draw the workflow as a Mermaid diagram.")
    ] = False,
    url: Url = DEFAULT_URL,
) -> None:
    """Show a token, its children and its history. With no ID, list the tokens. With
    --graph, draw the workflow."""
    if graph and token is not None:
        raise CommandError("give a token ID or --graph, not both")
    with Client(url) as client:
        if graph:
            typer.echo(mermaid(client.workflow()), nl=False)
            return
        if token is None:
            found = client.tokens()
            for each in found:
                typer.echo(token_line(each))
            if not found:
                typer.echo("no tokens")
            return
        typer.echo(token_line(client.get(token)))
        for child in client.tokens(parent=token):
            typer.echo("  child " + token_line(child))
        for entry in client.history(token):
            typer.echo(entry_line(entry))


@token_app.command("pause")
@reported
def pause(token: TokenId, url: Url = DEFAULT_URL) -> None:
    """Pause a token: its events are held, its tasks wait, its timers stand still."""
    _steered(url, lambda client: client.pause(token))


@token_app.command("resume")
@reported
def resume(token: TokenId, url: Url = DEFAULT_URL) -> None:
    """Resume a paused token: the held events apply in order."""
    _steered(url, lambda client: client.resume(token))


@token_app.command("cancel")
@reported
def cancel(token: TokenId, url: Url = DEFAULT_URL) -> None:
    """Cancel a token for good."""
    _steered(url, lambda client: client.cancel(token))


def _steered(url: str, act: Callable[[Client], dict[str, Any]]) -> None:
    with Client(url) as client:
        typer.echo(token_line(act(client)))


# ── how a token and its history look ────────────────────────────────────


def token_line(token: dict[str, Any]) -> str:
    """`T-1: coding, active`; `(visit 2)` after the state from the second visit."""
    visit = token.get("visit", 1)
    shown = f" (visit {visit})" if isinstance(visit, int) and visit > 1 else ""
    return f"{token['token']}: {_named(str(token['state']))}{shown}, {token['status']}"


def entry_line(entry: dict[str, Any]) -> str:
    """`HH:MM:SS  #<seq>  <what happened>`, in local time."""
    data = entry.get("data")
    facts = cast("dict[str, Any]", data) if isinstance(data, dict) else {}
    return f"{_local(str(entry['at']))}  #{entry['seq']}  {_what(str(entry['kind']), facts)}"


def json_object(text: str, name: str) -> dict[str, Any]:
    """A command-line argument as a JSON object; else one sentence that names it."""

    def constant(word: str) -> Any:
        raise ValueError(f"{word} is not a JSON value")

    try:
        value: Any = json.loads(text, parse_constant=constant)
    except json.JSONDecodeError as error:
        raise CommandError(
            f"{name} is not valid JSON: {error.msg} at column {error.colno}"
        ) from None
    except (ValueError, RecursionError) as error:
        raise CommandError(f"{name} is not valid JSON: {error}") from None
    if not isinstance(value, dict):
        raise CommandError(f'{name} must be a JSON object, such as \'{{"repo": "acme/api"}}\'')
    return cast("dict[str, Any]", value)


def _what(kind: str, data: dict[str, Any]) -> str:
    def get(key: str) -> str:
        return str(data.get(key, "?"))

    match kind:
        case "started" if "parent" in data:
            return f"started by {get('parent')}"
        case "started":
            return "started"
        case "branch_started":
            return f"started the child {get('token')}"
        case "entered":
            visit = data.get("visit", 1)
            again = f" (visit {visit})" if isinstance(visit, int) and visit > 1 else ""
            return f"entered {_named(get('state'))}{again}"
        case "task_queued":
            return f"task {get('task')} queued for {get('state')}"
        case "task_claimed":
            return f"{get('supervisor')} took task {get('task')} (attempt {get('attempt')})"
        case "task_done":
            return f"task {get('task')} done"
        case "task_failed":
            return f"task {get('task')} failed: {get('error')}"
        case "task_retrying":
            return f"task {get('task')} is tried again (attempt {get('attempt')})"
        case "task_cancelled":
            return f"task {get('task')} cancelled"
        case "task_waiting":
            return f"task {get('task')} waits: the work goes on outside"
        case "event_accepted":
            return f"{get('event')} in {_named(get('state'))}{_payload(data.get('payload'))}"
        case "event_rejected":
            return f"{get('event')} refused: {get('reason')}"
        case "timer_set":
            return f"timer set: {get('event')} at {_local(get('due_at'), '%Y-%m-%d %H:%M:%S')}"
        case "timer_fired":
            return f"timer fired: {get('event')}"
        case "note":
            return get("text")
        case "memory_set":
            return f"memory set: {get('memory_key')} (in {get('state')})"
        case "memory_deleted":
            return f"memory deleted: {get('memory_key')} (in {get('state')})"
        case "context_changed":
            keys = data.get("keys")
            names = cast("list[Any]", keys) if isinstance(keys, list) else ["?"]
            return "context changed: " + ", ".join(str(k) for k in names)
        case "cancelled" if "reason" in data:
            return f"cancelled: {get('reason')}"
        case "paused" | "resumed" | "completed" | "cancelled":
            return kind
        case "event_deferred":
            return f"{get('event')} held while paused"
        case "event_replayed":
            return f"{get('event')} applied on resume"
        case "event_dropped":
            return f"{get('event')} dropped on resume: {get('reason')}"
        case "failed":
            return f"failed: {get('reason')}"
        case _:
            return f"{kind} {_short(data)}"


def _named(state: str) -> str:
    """A state as a person reads it: Stateous's own as START and END."""
    return {"__start__": "START", "__end__": "END"}.get(state, state)


def _payload(payload: Any) -> str:
    return f": {_short(payload)}" if payload else ""


def _short(value: Any, limit: int = 80) -> str:
    text = json.dumps(value, ensure_ascii=True, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _local(at: str, form: str = "%H:%M:%S") -> str:
    """An ISO 8601 time in local time; the text as it is if it is not a time."""
    try:
        return datetime.fromisoformat(at).astimezone().strftime(form)
    except ValueError:
        return at
