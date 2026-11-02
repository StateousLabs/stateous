"""`WaitingClaims`: safe to close from a signal handler on the loop's own thread."""

import threading

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
