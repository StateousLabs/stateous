"""Delivering notices: each to the developer's handler at least once, in order for one
token family, and deleted only when the handler returns."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from stateous import ControlPlane, NoticesOff
from stateous.control.notices import Notice
from stateous.control.store import Store
from stateous.model.wire import Outcome

from .support import TICKET, sdlc, sqlite_url


def with_notices(url: str) -> None:
    store = Store(url)
    store.migrate(notices=True)
    store.close()


@pytest.fixture
def plane(tmp_path: Path) -> Iterator[ControlPlane]:
    url = sqlite_url(tmp_path / "a.db")
    with_notices(url)
    with ControlPlane(sdlc(), database=url) as plane:
        yield plane


def end_at_once(plane: ControlPlane, token: str) -> None:
    """The token starts, and its routine reports `stuck`: an event, then the end."""
    plane.start(token, TICKET)
    plane.fire(token, "stuck", None)


def test_deliver_hands_each_notice_once_and_deletes_it(plane: ControlPlane) -> None:
    end_at_once(plane, "T-1")
    got: list[Notice] = []

    assert plane.deliver_notices(got.append) == 2
    assert plane.deliver_notices(got.append) == 0
    assert [(n.token, n.kind) for n in got] == [("T-1", "event"), ("T-1", "completed")]
    assert got[0].workflow == "sdlc"


def test_notices_of_one_token_arrive_in_order(plane: ControlPlane) -> None:
    plane.start("T-1", TICKET)
    grant = plane.claim("box-1", ["coding"])
    assert grant is not None
    plane.report(grant.task, Outcome(kind="event", event="done"), supervisor="box-1")
    plane.fire("T-1", "passed", {"passed": 1, "failed": 0})
    got: list[Notice] = []

    plane.deliver_notices(got.append)

    assert [(n.kind, n.event) for n in got] == [
        ("event", "done"),
        ("event", "passed"),
        ("completed", None),
    ]


def test_a_failing_handler_gets_the_same_notices_again_in_order(plane: ControlPlane) -> None:
    end_at_once(plane, "A-1")
    end_at_once(plane, "B-1")
    got: list[tuple[str, str]] = []

    def fails_on_the_second_of_a(notice: Notice) -> None:
        if notice.token == "A-1" and notice.kind == "completed":
            raise RuntimeError("the queue is down")
        got.append((notice.token, notice.kind))

    assert plane.deliver_notices(fails_on_the_second_of_a) == 2  # B-1's two only
    assert got == [("A-1", "event"), ("B-1", "event"), ("B-1", "completed")]

    again: list[tuple[str, str]] = []
    assert plane.deliver_notices(lambda n: again.append((n.token, n.kind))) == 2
    assert again == [("A-1", "event"), ("A-1", "completed")]


def test_the_limit_stops_the_delivery(plane: ControlPlane) -> None:
    end_at_once(plane, "T-1")
    end_at_once(plane, "T-2")
    got: list[Notice] = []

    assert plane.deliver_notices(got.append, limit=3) == 3
    assert plane.deliver_notices(got.append, limit=3) == 1


def test_deliver_refuses_when_notices_are_off(tmp_path: Path) -> None:
    with (
        ControlPlane(sdlc(), database=sqlite_url(tmp_path / "off.db"), migrate=True) as plane,
        pytest.raises(NoticesOff, match="run: stateous migrate --db URL --notices"),
    ):
        plane.deliver_notices(lambda notice: None)

    assert NoticesOff.code == "notices_off"


def test_a_notice_id_is_stable_across_redelivery(plane: ControlPlane) -> None:
    end_at_once(plane, "T-1")
    first: list[str] = []

    def fail_after_reading(notice: Notice) -> None:
        first.append(notice.id)
        raise RuntimeError("lost")

    plane.deliver_notices(fail_after_reading)
    second: list[str] = []
    plane.deliver_notices(lambda notice: second.append(notice.id))

    assert first[0] == second[0]
    assert first[0].startswith("sdlc:")


def test_start_b_from_a_is_safe_to_repeat(plane: ControlPlane, tmp_path: Path) -> None:
    with ControlPlane(sdlc(), database=sqlite_url(tmp_path / "b.db"), migrate=True) as b:
        end_at_once(plane, "A-1")
        crash = True

        def start_b(notice: Notice) -> None:
            nonlocal crash
            if notice.kind == "completed":
                b.start(f"b-of-{notice.token}", TICKET)
                if crash:
                    crash = False
                    raise RuntimeError("the process died before the commit")

        plane.deliver_notices(start_b)
        plane.deliver_notices(start_b)

        assert [token.token for token in b.tokens()] == ["b-of-A-1"]
        assert plane.deliver_notices(start_b) == 0


def test_run_notices_delivers_until_close(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "run.db")
    with_notices(url)
    plane = ControlPlane(sdlc(), database=url)
    got: list[Notice] = []
    done = threading.Event()

    def handler(notice: Notice) -> None:
        got.append(notice)
        if notice.kind == "completed":
            done.set()

    async def main() -> None:
        running = asyncio.create_task(plane.run_notices(handler, every=0.01))
        end_at_once(plane, "T-1")
        while not done.is_set():
            await asyncio.sleep(0.01)
        plane.close()
        await asyncio.wait_for(running, timeout=5)

    asyncio.run(main())

    assert [n.kind for n in got] == ["event", "completed"]


def test_notice_ids_do_not_repeat_after_a_delivery(plane: ControlPlane) -> None:
    end_at_once(plane, "T-1")
    first: list[str] = []
    plane.deliver_notices(lambda notice: first.append(notice.id))
    end_at_once(plane, "T-2")
    second: list[str] = []

    plane.deliver_notices(lambda notice: second.append(notice.id))

    assert len(second) == 2
    assert set(first).isdisjoint(second)


def test_run_notices_goes_on_after_a_database_error(
    plane: ControlPlane, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real = plane.deliver_notices
    calls = 0

    def once_broken(handler: Callable[[Notice], None], *, limit: int = 100) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("SELECT 1", {}, Exception("the server closed the connection"))
        return real(handler, limit=limit)

    monkeypatch.setattr(plane, "deliver_notices", once_broken)
    got: list[Notice] = []
    end_at_once(plane, "T-1")

    async def main() -> None:
        running = asyncio.create_task(plane.run_notices(got.append, every=0.01))
        try:
            for _ in range(500):  # at most 5 seconds
                if len(got) >= 2 or running.done():
                    break
                await asyncio.sleep(0.01)
        finally:
            plane.close()
        await asyncio.wait_for(running, timeout=5)

    asyncio.run(main())

    assert [n.kind for n in got] == ["event", "completed"]
    assert "the notices could not be delivered" in caplog.text


def test_the_control_plane_turns_notices_on(tmp_path: Path) -> None:
    with ControlPlane(sdlc(), database=sqlite_url(tmp_path / "m.db"), migrate=True) as plane:
        plane.migrate(notices=True)
        end_at_once(plane, "T-1")

        assert plane.deliver_notices(lambda notice: None) == 2
