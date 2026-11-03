"""Notices and "task ready" carry the token's labels."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from stateous import ControlPlane, Notice
from stateous.control.store import Store

from .support import TICKET, sdlc, sqlite_url

U1 = {"user": "u1"}


def test_a_notice_carries_the_tokens_labels(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "n.db")
    store = Store(url)
    store.migrate(notices=True)
    store.close()
    with ControlPlane(sdlc(), database=url) as plane:
        plane.start("T-1", TICKET, labels=U1)
        plane.fire("T-1", "stuck", None)
        got: list[Notice] = []

        plane.deliver_notices(got.append)

    assert [n.labels for n in got] == [U1, U1]


def test_ready_carries_the_tokens_labels(tmp_path: Path) -> None:
    heard: list[tuple[str, dict[str, str]]] = []

    def hear(state: str, labels: Mapping[str, str]) -> None:
        heard.append((state, dict(labels)))

    with ControlPlane(sdlc(), database=sqlite_url(tmp_path / "r.db"), migrate=True) as plane:
        plane.on_task_ready(hear)
        plane.start("T-1", TICKET, labels=U1)

    assert heard == [("coding", U1)]
