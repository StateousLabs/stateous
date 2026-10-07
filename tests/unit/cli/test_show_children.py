"""`stateous token show ID` lists the token's children: up to the API's largest
listing, and it says so when the list is cut."""

from __future__ import annotations

from types import TracebackType
from typing import Any

import pytest
from typer.testing import CliRunner

from stateous.cli import tokens
from stateous.cli.main import app

asked: list[int | None] = []


class Listing:
    """A control plane whose token `T-1` has `children` children."""

    children = 0

    def __init__(self, url: str) -> None:
        self.url = url

    def __enter__(self) -> Listing:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        return None

    def get(self, token: str) -> dict[str, Any]:
        return {"token": token, "state": "reviews", "status": "active", "visit": 1}

    def tokens(
        self, *, status: str | None = None, parent: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        asked.append(limit)
        count = min(Listing.children, limit or 100)
        return [
            {"token": f"T-1.reviews.1.w{n}", "state": "triage", "status": "active", "visit": 1}
            for n in range(count)
        ]

    def history(self, token: str) -> list[dict[str, Any]]:
        return []


@pytest.fixture(autouse=True)
def listing(monkeypatch: pytest.MonkeyPatch) -> None:
    asked.clear()
    monkeypatch.setattr(tokens, "Client", Listing)


def shown(children: int) -> str:
    Listing.children = children
    result = CliRunner().invoke(app, ["token", "show", "T-1"])
    assert result.exit_code == 0, result.output
    return result.stdout


def test_show_lists_more_than_100_children() -> None:
    output = shown(150)

    assert output.count("  child ") == 150
    assert "there can be more" not in output
    assert asked == [1000]


def test_show_says_when_the_list_of_children_is_cut() -> None:
    output = shown(1500)

    assert output.count("  child ") == 1000
    assert "  (only the 1000 most recently moved children are shown; there can be more)" in output
