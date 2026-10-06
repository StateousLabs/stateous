"""Where `serve` listens, and when it warns."""

import pytest

from stateous.cli.serving import ServeRefused, host_and_port, listen_warning


@pytest.mark.parametrize(
    ("listen", "expected"),
    [
        ("127.0.0.1:8080", ("127.0.0.1", 8080)),
        ("8080", ("127.0.0.1", 8080)),
        (":9000", ("127.0.0.1", 9000)),
        ("0.0.0.0:80", ("0.0.0.0", 80)),
        ("[::1]:8080", ("::1", 8080)),
        ("localhost:8080", ("localhost", 8080)),
        ("127.0.0.1:0", ("127.0.0.1", 0)),
    ],
)
def test_listen_is_a_host_and_a_port(listen: str, expected: tuple[str, int]) -> None:
    assert host_and_port(listen) == expected


@pytest.mark.parametrize("listen", ["0.0.0.0", "host:", "host:65536", "::1:8080", "[x]:80", "a:b"])
def test_a_listen_that_is_not_host_and_port_is_one_sentence(listen: str) -> None:
    with pytest.raises(ServeRefused, match="is not HOST:PORT"):
        host_and_port(listen)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_a_local_host_gives_no_warning(host: str) -> None:
    assert listen_warning(host) is None


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "cp.internal"])
def test_a_wider_host_warns_that_there_is_no_authentication(host: str) -> None:
    warning = listen_warning(host)

    assert warning is not None
    assert warning.startswith("warning: ")
    assert "no authentication" in warning
    assert "\n" not in warning
