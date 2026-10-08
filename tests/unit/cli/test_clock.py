"""The control plane's clock: it renews the hold and ticks; it stops when the hold
is lost; a failed tick or an unreachable database does not stop it. After a lost
hold, the app answers 503 to each request."""

import asyncio
import time
from collections.abc import Callable

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from stateous.cli.serving import Clock, ClockedApp


class Ticks:
    def __init__(self, failures: int = 0) -> None:
        self.count = 0
        self.failures = failures

    def tick(self) -> int:
        self.count += 1
        if self.count <= self.failures:
            raise RuntimeError("database gone")
        return 0


class Hold:
    """Answers `keep_serving` from a list; the last answer repeats."""

    def __init__(self, answers: list[bool | Exception]) -> None:
        self.answers = answers
        self.asked = 0

    def keep_serving(self) -> bool:
        self.asked += 1
        answer = self.answers[min(self.asked, len(self.answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer


def run_until(clock: Clock, done: Callable[[], bool], within: float = 5.0) -> None:
    """Run the clock until `done()` is true, or until the clock ends by itself. Then
    stop it, and wait until it ended."""

    async def main() -> None:
        ticking = asyncio.create_task(clock.run())
        deadline = time.monotonic() + within
        while not done() and not ticking.done():
            assert time.monotonic() < deadline, "the condition did not come true in time"
            await asyncio.sleep(0.01)
        clock.stop()
        await asyncio.wait_for(ticking, timeout=5)

    asyncio.run(main())


def test_the_clock_ticks_again_and_again_until_it_stops() -> None:
    ticks = Ticks()
    clock = Clock(ticks, Hold([True]), every=0.01)

    run_until(clock, lambda: ticks.count >= 3)

    assert ticks.count >= 3
    assert not clock.lost


def test_a_tick_that_fails_is_logged_and_the_clock_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ticks = Ticks(failures=2)
    clock = Clock(ticks, Hold([True]), every=0.01)

    run_until(clock, lambda: ticks.count >= 4)

    assert "tick failed (database gone)" in caplog.text
    assert "Traceback" not in caplog.text  # one line, never a traceback


def test_a_lost_hold_stops_the_clock_and_says_so_once(caplog: pytest.LogCaptureFixture) -> None:
    ticks = Ticks()
    clock = Clock(ticks, Hold([False]), every=0.01)

    run_until(clock, lambda: clock.lost)

    assert clock.lost
    assert ticks.count == 0
    assert caplog.text.count("another control plane took this database") == 1


def test_a_database_that_does_not_answer_does_not_stop_the_clock(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ticks = Ticks()
    clock = Clock(ticks, Hold([OSError("unreachable"), True]), every=0.01)

    run_until(clock, lambda: ticks.count >= 2)

    assert "cannot renew" in caplog.text
    assert not clock.lost


def test_a_renewal_that_raises_skips_the_tick_for_that_step(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A renewal that raises every time is not a lost hold: the clock skips the
    engine's tick for that step, and never sets `lost`."""
    ticks = Ticks()
    clock = Clock(ticks, Hold([OSError("unreachable")]), every=0.01)

    async def main() -> None:
        ticking = asyncio.create_task(clock.run())
        await asyncio.sleep(0.1)
        clock.stop()
        await asyncio.wait_for(ticking, timeout=5)

    asyncio.run(main())

    assert ticks.count == 0
    assert not clock.lost
    assert "cannot renew" in caplog.text


def test_after_a_lost_hold_each_request_is_503() -> None:
    clock = Clock(Ticks(), Hold([False]), every=0.01)
    run_until(clock, lambda: clock.lost)

    answer = TestClient(ClockedApp(Starlette(), clock)).get("/health")

    assert answer.status_code == 503
    assert "another control plane took this database" in answer.json()["error"]
    assert "stop this server" in answer.json()["error"]
    assert "try again" not in answer.json()["error"]
    assert answer.json()["code"] == "not_serving"


def test_a_lost_hold_calls_on_lost_once_after_the_line(caplog: pytest.LogCaptureFixture) -> None:
    lines: list[int] = []  # how many lost lines were logged when on_lost ran
    clock = Clock(Ticks(), Hold([False]), every=0.01)
    clock.on_lost = lambda: lines.append(caplog.text.count("another control plane took"))

    run_until(clock, lambda: clock.lost)

    assert lines == [1]


def test_a_kept_hold_never_calls_on_lost() -> None:
    calls: list[None] = []
    ticks = Ticks()
    clock = Clock(ticks, Hold([True]), every=0.01, on_lost=lambda: calls.append(None))

    run_until(clock, lambda: ticks.count >= 3)

    assert calls == []
