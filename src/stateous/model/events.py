"""Occurrences: one thing that happened, with its payload.

An `Event` (in workflow.py) declares a kind of outcome. An occurrence is one
instance of it. A generated contract has one `Occurrence` subclass per event, so
`events.failed(report)` is typed by the event's payload.
"""

from __future__ import annotations

from typing import Any, ClassVar, Generic, TypeVar, cast, get_args, get_origin, overload

from pydantic import BaseModel

P = TypeVar("P", bound=BaseModel | None)


class Occurrence(Generic[P]):
    """One occurrence of an event, with its payload. A routine returns one.

    The generated contract has one subclass for each event, in the class `events`.
    The type argument is the payload model, or `None`. For example,
    `events.failed(Tests(passed=3, failed=1))` is an `Occurrence[Tests]`, and
    `events.done()` is an `Occurrence[None]`."""

    name: ClassVar[str]
    """The event's name. Each subclass sets it."""
    payload: P
    """The payload, or None for an event without a payload."""

    @overload
    def __init__(self: Occurrence[None]) -> None: ...
    @overload
    def __init__(self, payload: P) -> None: ...
    def __init__(self, payload: P | None = None) -> None:
        self.payload = cast("P", payload)

    @classmethod
    def payload_model(cls) -> type[BaseModel] | None:
        """The payload model of the event.

        Returns:
            The model of the class's `Occurrence[Model]` base, or None when the
                event has no payload.
        """
        for klass in cls.__mro__:
            bases: tuple[Any, ...] = getattr(klass, "__orig_bases__", ())
            for base in bases:
                if get_origin(base) is Occurrence:
                    (argument,) = get_args(base)
                    if isinstance(argument, type) and issubclass(argument, BaseModel):
                        return argument
                    return None
        return None

    def __repr__(self) -> str:
        return f"{self.name}({self.payload!r})"
