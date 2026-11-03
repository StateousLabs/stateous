"""`stateous token show ID` prints the token's context before its history, and
`--json` prints the token, its children and its history as one JSON object."""

from __future__ import annotations

import json
from types import TracebackType
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from stateous.cli import tokens
from stateous.cli.main import app


class OneToken:
    """A control plane with one token, `T-1`, whose context is `context`."""

    context: ClassVar[dict[str, Any]] = {}

    def __init__(self, url: str) -> None:
        self.url = url

    def __enter__(self) -> OneToken:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        return None

    def get(self, token: str) -> dict[str, Any]:
        return {
            "token": token, "state": "answer", "status": "completed", "visit": 1,
            "context": OneToken.context,
        }  # fmt: skip

    def tokens(
        self, *, status: str | None = None, parent: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        return []

    def history(self, token: str) -> list[dict[str, Any]]:
        return [{"seq": 1, "at": "2026-11-01T10:00:00+00:00", "kind": "started", "data": {}}]


@pytest.fixture(autouse=True)
def one_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokens, "Client", OneToken)


def shown(context: dict[str, Any], *options: str) -> str:
    OneToken.context = context
    result = CliRunner().invoke(app, ["token", "show", "T-1", *options])
    assert result.exit_code == 0, result.output
    return result.stdout


def test_show_prints_the_context_before_the_history() -> None:
    lines = shown({"title": "first", "reply": "thanks"}).splitlines()

    at = lines.index("context:")
    assert lines[at + 1] == "  {"
    assert '    "title": "first",' in lines
    assert '    "reply": "thanks"' in lines
    first_entry = next(n for n, line in enumerate(lines) if "#1" in line)
    assert at < first_entry


def test_show_prints_an_empty_context() -> None:
    assert "context: {}" in shown({}).splitlines()


def test_show_json_is_the_token_its_children_and_its_history() -> None:
    printed = json.loads(shown({"title": "first"}, "--json"))

    assert set(printed) == {"token", "children", "history"}
    assert printed["token"]["context"] == {"title": "first"}
    assert printed["children"] == []
    assert printed["history"][0]["kind"] == "started"


def test_json_needs_a_token_id() -> None:
    result = CliRunner().invoke(app, ["token", "show", "--json"])

    assert result.exit_code == 1
    assert "--json needs a token ID" in result.output


def test_token_show_prints_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    class Labeled(OneToken):
        def get(self, token: str) -> dict[str, Any]:
            return {**super().get(token), "labels": {"user": "u1", "project": "p7"}}

    monkeypatch.setattr(tokens, "Client", Labeled)

    result = CliRunner().invoke(app, ["token", "show", "T-1"])

    assert "labels: project=p7, user=u1" in result.output


def test_token_start_takes_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[dict[str, str] | None] = []

    class Starting(OneToken):
        def start(
            self, token: str, context: dict[str, Any], *, labels: dict[str, str] | None = None
        ) -> dict[str, Any]:
            started.append(labels)
            return self.get(token)

    monkeypatch.setattr(tokens, "Client", Starting)

    result = CliRunner().invoke(
        app, ["token", "start", "T-1", "--label", "user=u1", "--label", "project=p7"]
    )

    assert result.exit_code == 0, result.output
    assert started == [{"user": "u1", "project": "p7"}]
