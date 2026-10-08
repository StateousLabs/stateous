"""The host of a control-plane URL: one check, used wherever a URL names a
control plane (`stateous.yaml`'s `url`, `Client`, and every CLI command that
takes `--url`, including `init URL`). Without it, a typo such as a space or a
stray `%` in the host passes every URL check — httpx2 accepts it, encoding the
space to `%20` — and only fails at connect time, which reads like an outage and
is retried as one.

A host is valid when it is an IP address (IPv4, or IPv6 written with or without
the brackets a URL puts around it), or a name of one or more labels — letters,
digits, `-` and `_` — separated by `.`. `localhost` is such a name; a dot is
not required, and a trailing dot is allowed. `_` is not valid in a host name
per RFC 1123, but some setups use it for a Docker service name, so it is
allowed here too.

A host with a space or a `%` is refused, and so is one with a non-ASCII (IDN)
letter: an IDN host is a real concern on its own (a lookalike domain, not only
a typo), so it is refused rather than let through.
"""

from __future__ import annotations

import ipaddress
import re

HOST_RULE = "a host has only letters, digits, '-' and '.', or is an IP address"
"""The rule, as every refusal states it: `host_problem`'s result for a bad host."""

_LABEL = re.compile(r"^[A-Za-z0-9_-]+$")


def host_problem(host: str) -> str | None:
    """Why `host` is not a valid control-plane host, or None when it is valid.
    Checked on the host as written: a space or a `%` is refused here, whether or
    not the URL it came from already turned it into `%20` or kept it as it was."""
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if _is_ip_address(bare):
        return None
    without_trailing_dot = bare[:-1] if bare.endswith(".") else bare
    labels = without_trailing_dot.split(".")
    if not labels or any(_LABEL.fullmatch(label) is None for label in labels):
        return HOST_RULE
    return None


def _is_ip_address(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True
