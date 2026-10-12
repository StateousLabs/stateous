"""`configure_logging`: the `stateous` logger stays at INFO, because the
supervisor reads INFO lines from `serve`."""

import logging

from stateous.cli.serving import configure_logging


def test_the_stateous_logger_stays_at_info() -> None:
    logging.getLogger("stateous").setLevel(logging.WARNING)

    configure_logging()

    assert logging.getLogger("stateous").level == logging.INFO


def test_configure_logging_is_safe_to_call_more_than_once() -> None:
    configure_logging()
    configure_logging()

    assert logging.getLogger("stateous").level == logging.INFO
