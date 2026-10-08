"""The host of a control-plane URL: one rule, shared by `stateous.yaml`'s `url`,
`Client`, and every CLI command that takes `--url`."""

import pytest

from stateous.model.host import HOST_RULE, host_problem


def test_the_rule_names_letters_digits_dash_dot_or_an_ip() -> None:
    assert HOST_RULE == "a host has only letters, digits, '-' and '.', or is an IP address"


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "cp.internal",
        "127.0.0.1",
        "::1",
        "[::1]",
        "example.com.",  # a trailing dot is allowed
        "my_host",  # '_' is not RFC 1123, but Docker uses it: allowed
        "a-b.c-d",
    ],
)
def test_a_valid_host_has_no_problem(host: str) -> None:
    assert host_problem(host) is None


@pytest.mark.parametrize(
    "host",
    [
        "my host",  # a space
        "local%host",  # a '%'
        "my%20host",  # a space already encoded
        "exämple.com",  # non-ASCII (IDN): a concern on its own
        "",
        "host..name",  # an empty label
    ],
)
def test_an_invalid_host_is_the_rule(host: str) -> None:
    assert host_problem(host) == HOST_RULE
