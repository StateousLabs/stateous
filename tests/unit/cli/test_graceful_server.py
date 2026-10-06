"""`GracefulServer`: a first signal stops it gracefully; a second forces it."""

import signal

import uvicorn
from starlette.applications import Starlette

from stateous.cli.serving import GracefulServer


def server() -> GracefulServer:
    return GracefulServer(uvicorn.Config(Starlette()))


def test_the_first_signal_asks_for_a_graceful_stop() -> None:
    graceful = server()

    graceful.handle_exit(signal.SIGINT, None)

    assert graceful.should_exit
    assert not graceful.force_exit


def test_a_second_signal_forces_it() -> None:
    graceful = server()
    graceful.handle_exit(signal.SIGINT, None)

    graceful.handle_exit(signal.SIGINT, None)

    assert graceful.should_exit
    assert graceful.force_exit
