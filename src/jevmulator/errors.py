"""HTTP error shapes.

422 uses the pinned ``HTTPValidationError`` body: ``{"detail": [ValidationError, ...]}``
with each entry carrying ``loc``, ``msg`` and ``type``.

Every other status uses ``{"detail": {"error_type": ..., "message": ...}}``. The object
form is what the retained ``jev-review`` consumer reads (``detail.error_type``) and is one
of the envelopes both official SDK error parsers accept. TypeSafe's exact non-422 bodies
are unverified, so this is a Jevmulator policy and not a parity claim.
"""

from __future__ import annotations

from typing import Any


class JevmulatorError(Exception):
    """Base class for an error that maps onto an HTTP response."""

    status = 500
    error_type = "internal_error"

    def __init__(self, message: str, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.headers = dict(headers or {})

    def body(self) -> dict[str, Any]:
        return {"detail": {"error_type": self.error_type, "message": self.message}}


class RequestValidationError(JevmulatorError):
    """A request the pinned schema rejects. Emits the pinned 422 body."""

    status = 422
    error_type = "validation_error"

    def __init__(self, details: list[dict[str, Any]]) -> None:
        first = details[0]["msg"] if details else "Request validation failed"
        super().__init__(first)
        self.details = details

    def body(self) -> dict[str, Any]:
        return {"detail": self.details}


def validation_detail(
    loc: list[Any], msg: str, type_: str, *, input_: Any = None, ctx: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build one pinned ``ValidationError`` entry."""
    detail: dict[str, Any] = {"loc": loc, "msg": msg, "type": type_}
    if input_ is not None:
        detail["input"] = input_
    if ctx is not None:
        detail["ctx"] = ctx
    return detail


class UnauthorizedError(JevmulatorError):
    status = 401
    error_type = "authentication_error"

    def __init__(self, message: str = "Invalid or missing API key.") -> None:
        super().__init__(message, headers={"WWW-Authenticate": "Bearer"})


class NotFoundError(JevmulatorError):
    status = 404
    error_type = "not_found"


class MethodNotAllowedError(JevmulatorError):
    status = 405
    error_type = "method_not_allowed"


class RequestTooLargeError(JevmulatorError):
    status = 413
    error_type = "request_too_large"


class MaxTokensExceededError(RequestValidationError):
    """The optional approximate size guard rejected the request.

    The code ``max_tokens_exceeded`` is the string the retained ``jev-review`` consumer
    looks for. The guard counts characters, not TypeSafe tokens.
    """

    def __init__(self, message: str, loc: list[Any]) -> None:
        super().__init__(
            [validation_detail(loc, message, "max_tokens_exceeded")]
        )
        self.error_type = "max_tokens_exceeded"

    def body(self) -> dict[str, Any]:
        return {"detail": {"error_type": "max_tokens_exceeded", "message": self.message}}


class RequestTimeoutError(JevmulatorError):
    """The client declared a body and did not finish sending it in time.

    Without this the handler would block on a socket read for as long as the client
    chose, which occupies one handler thread indefinitely and never reaches the
    evaluation deadline.
    """

    status = 408
    error_type = "request_timeout"


class TooManyRequestsError(JevmulatorError):
    status = 429
    error_type = "too_many_requests"


class RateLimitedError(JevmulatorError):
    """The upstream provider rate limited this daemon."""

    status = 429
    error_type = "rate_limit_error"


class OverloadedError(JevmulatorError):
    """The upstream provider reported 503 or 529."""

    status = 529
    error_type = "overloaded"


class UpstreamError(JevmulatorError):
    """The upstream provider failed in a way this daemon cannot repair."""

    status = 502
    error_type = "upstream_error"


class UpstreamInvalidOutputError(UpstreamError):
    """Upstream output could not be turned into a valid answer."""

    error_type = "upstream_invalid_output"


class UpstreamRefusalError(UpstreamError):
    """The upstream model refused to answer."""

    error_type = "upstream_refusal"


class UsageUnavailableError(UpstreamError):
    """Strict usage policy: upstream reported no token counts."""

    error_type = "usage_unavailable"


class UpstreamTimeoutError(JevmulatorError):
    status = 504
    error_type = "upstream_timeout"


class UpstreamNotConfiguredError(JevmulatorError):
    """No upstream credential is available, so no judgment can be produced."""

    status = 502
    error_type = "upstream_not_configured"
