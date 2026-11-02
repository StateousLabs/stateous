"""How one attempt of a call fails: refused, or not reached."""


class Refused(Exception):
    """The control plane answered, and refused the call. `code` is the stable code of
    the refusal (see the docs, Reference > The HTTP API); `message` is for people. To try
    again does not help."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class NotReached(Exception):
    """No answer came, or an answer that means "try again". The call may or may not
    have arrived: the supervisor sends it again, with the same request id."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why
