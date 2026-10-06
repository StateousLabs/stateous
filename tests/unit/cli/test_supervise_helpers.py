"""The helpers of `stateous supervise`: its log lines, and a loop that ends even when
a routine does not stop."""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from stateous.cli.running import log_to_stderr, run_bounded
from stateous.cli.serving import configure_logging


def test_log_to_stderr_adds_no_second_handler() -> None:
    configure_logging()
    handlers = len(logging.getLogger().handlers)

    log_to_stderr()
    log_to_stderr()

    assert len(logging.getLogger().handlers) == handlers
    assert logging.getLogger("stateous").level == logging.INFO
    assert logging.getLogger("httpx2").level == logging.WARNING


def test_the_loop_ends_although_a_task_ignores_its_cancellation() -> None:
    async def stubborn() -> None:
        for _ in range(100):
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                continue  # a routine that swallows its cancellation

    async def main() -> int:
        asyncio.get_running_loop().create_task(stubborn())
        await asyncio.sleep(0)
        return 1

    began = time.monotonic()
    code, _ = run_bounded(main(), leftover_wait=0.2)

    assert code == 1
    assert time.monotonic() - began < 3


def test_the_loop_returns_the_code_of_main() -> None:
    async def main() -> int:
        return 0

    assert run_bounded(main(), leftover_wait=0.2) == (0, True)


def test_the_end_is_not_clean_while_a_thread_of_the_loop_still_runs() -> None:
    release = threading.Event()

    async def main() -> int:
        asyncio.get_running_loop().run_in_executor(None, release.wait, 30)
        await asyncio.sleep(0)
        return 0

    began = time.monotonic()
    try:
        result = run_bounded(main(), leftover_wait=0.2)
    finally:
        release.set()

    assert result == (0, False)
    assert time.monotonic() - began < 3


def test_the_end_is_clean_when_the_threads_of_the_loop_are_done() -> None:
    async def main() -> int:
        await asyncio.to_thread(time.sleep, 0)
        return 0

    assert run_bounded(main(), leftover_wait=0.2) == (0, True)
