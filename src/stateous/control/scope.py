"""Token ids for each scope: the key that Stateous keeps, and the id that a caller sees.

In a scoped namespace a token is kept under `<digest>:<id>`. The digest is the first
16 hex characters of SHA-256 over the values of the scope's labels. `:` cannot occur
in an id, so the first `:` always ends the digest. With no scope, the key is the id."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


def scope_digest(values: Mapping[str, str]) -> str:
    """The digest of a scope's values: the same for the same values, in any order."""
    canonical = json.dumps(dict(values), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def key_of(digest: str | None, token: str) -> str:
    """The key that Stateous keeps for `token` in the scope of `digest`."""
    return token if digest is None else f"{digest}:{token}"


def public_id(key: str) -> str:
    """The id that a caller sees: the key without its digest."""
    return key.partition(":")[2] or key


def digest_of(key: str) -> str | None:
    """The digest of a key; None for a key with no scope."""
    digest, colon, _ = key.partition(":")
    return digest if colon else None
