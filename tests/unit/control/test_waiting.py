"""`WaitingClaims`: safe to close from a signal handler on the loop's own thread."""

import asyncio
import threading
from collections.abc import Callable, Mapping

from stateous.control.waiting import WaitingClaims


def test_close_does_not_deadlock_while_the_same_thread_holds_the_set() -> None:
    """A signal can run `close` on the thread that is inside `waiting` right now."""
    waiting = WaitingClaims()
    done = threading.Event()

    def run() -> None:
        with waiting._lock:  # pyright: ignore[reportPrivateUsage]
            waiting.close()
        done.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=5)

    assert done.is_set(), "close() deadlocked"


def test_the_set_hears_ready_only_while_claims_wait() -> None:
    subscribed: list[Callable[[str, Mapping[str, str]], None]] = []

    def subscribe(callback: Callable[[str, Mapping[str, str]], None]) -> Callable[[], None]:
        subscribed.append(callback)
        return lambda: subscribed.remove(callback)

    waiting = WaitingClaims(subscribe)
    assert subscribed == []

    async def main() -> None:
        with waiting.waiting(["coding"]):
            assert len(subscribed) == 1
            with waiting.waiting(["testing"]):
                assert len(subscribed) == 1
        assert subscribed == []

    asyncio.run(main())


def test_a_waiter_with_labels_wakes_only_for_matching_labels() -> None:
    waiting = WaitingClaims()

    async def main() -> tuple[bool, bool]:
        with waiting.waiting(["coding"], {"user": "a"}) as ready:
            waiting.ready("coding", {"user": "b"})
            await asyncio.sleep(0.05)
            other = ready.is_set()
            waiting.ready("coding", {"user": "a", "project": "p"})
            await asyncio.sleep(0.05)
            return other, ready.is_set()

    assert asyncio.run(main()) == (False, True)
