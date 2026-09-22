"""Provider interface and the exceptions a provider may raise.

A provider turns a list of chat messages plus an output schema into decoded JSON text
and the upstream token counts. It performs no retries and makes no judgment of its own;
the evaluator owns both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class UpstreamCall:
    """One request this daemon sent upstream, as recorded for the debug route.

    ``messages`` is the exact list sent. The isolation tests read this to prove that no
    question ID and no sibling question ever reached the model.
    """

    model: str
    messages: list[dict[str, str]]
    response_format: str
    schema: dict[str, Any] | None = None


@dataclass
class ProviderResult:
    """One upstream answer.

    ``input_tokens`` and ``output_tokens`` are ``None`` when the provider reported no
    usage. The evaluator decides what to do with that, and never invents a count.
    """

    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None
    raw_model: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ProviderTransportError(Exception):
    """A retryable failure: connection error, timeout, 408, 429 or 5xx."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after_seconds: float | None = None,
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after_seconds = retry_after_seconds
        self.timed_out = timed_out


class ProviderRequestError(Exception):
    """A non-retryable upstream failure, such as 400 or 401 from the provider."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class ProviderRefusalError(Exception):
    """The upstream model declined to answer."""


class Provider:
    """Base class. Subclasses implement :meth:`complete` and :meth:`close`."""

    name = "base"

    def complete(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        timeout: float,
    ) -> ProviderResult:
        raise NotImplementedError

    def close(self) -> None:
        """Release any resource the provider holds."""

    # -- recording -------------------------------------------------------

    def record(self, call: UpstreamCall) -> None:
        """Store one outgoing call when recording is enabled."""
        recorder = getattr(self, "_recorder", None)
        if recorder is not None:
            recorder(call)

    def set_recorder(self, recorder) -> None:
        self._recorder = recorder
