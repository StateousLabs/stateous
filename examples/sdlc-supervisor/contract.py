# GENERATED from the `sdlc` workflow — do not edit.
# sha256:ce35bf077678e6aa975cf404a1417ecdb9f5283b037a34d00d5418cbc9123881
from __future__ import annotations

from pydantic import BaseModel

from stateous import Context, Occurrence, StateRef

__workflow__ = "sdlc"
__checksum__ = "sha256:ce35bf077678e6aa975cf404a1417ecdb9f5283b037a34d00d5418cbc9123881"


class Tests(BaseModel):
    passed: int
    failed: int


class Ticket(BaseModel):
    repo: str


TicketContext = Context[Ticket]


class events:
    class done(Occurrence[None]):
        name = "done"

    class stuck(Occurrence[None]):
        name = "stuck"

    class passed(Occurrence[Tests]):
        name = "passed"

    class failed(Occurrence[Tests]):
        name = "failed"


class states:
    coding = StateRef("coding", emits=(events.done, events.stuck))
    testing = StateRef("testing", emits=(events.passed, events.failed))
