"""The distinct failure contracts: bad credential, missing right, bad input, conflict, authority down."""

from __future__ import annotations


class IamError(Exception):
    """Base class; ``status`` is the HTTP status a service adapter returns and ``code`` a stable slug."""

    status = 500
    code = "iam_error"

    def __init__(self, message: str = "", *, fields: tuple[str, ...] = ()) -> None:
        """Keep the message bounded and free of bearer material (callers never put tokens in it).

        ``fields`` are the malformed parts of a request, in full: the message is cut, the list never is.
        """
        super().__init__(message[:200])
        self.fields = fields


class Unauthenticated(IamError):  # noqa: N818 - names the HTTP condition
    """Invalid, expired, revoked or wrong-audience credential: recover it, do not request access."""

    status = 401
    code = "unauthenticated"


class Forbidden(IamError):  # noqa: N818 - names the HTTP condition
    """Authenticated but not allowed; deliberately does not say whether the target exists."""

    status = 403
    code = "forbidden"


class Invalid(IamError):  # noqa: N818 - names the HTTP condition
    """Malformed or out-of-policy input; nothing was mutated."""

    status = 400
    code = "invalid"


class Conflict(IamError):  # noqa: N818 - names the HTTP condition
    """Idempotency-key reuse with different content, or a decision that was already made."""

    status = 409
    code = "conflict"


class Unavailable(IamError):  # noqa: N818 - names the HTTP condition
    """The authority could not answer; services must execute nothing and must not report a missing grant."""

    status = 503
    code = "unavailable"
