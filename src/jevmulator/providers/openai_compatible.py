"""OpenAI-compatible Chat Completions provider.

Targets any endpoint that serves ``POST <base_url>/chat/completions`` with the OpenAI
request and response shape. The initial target is GLM Flash on z.ai, whose non-thinking
mode is the explicit extension field ``{"thinking": {"type": "disabled"}}``.

The provider sends the upstream API key in the ``Authorization`` header and nowhere else.
No code path here logs a key, a header, or a full request.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any

from .base import (
    Provider,
    ProviderRefusalError,
    ProviderRequestError,
    ProviderResult,
    ProviderTransportError,
    UpstreamCall,
)

#: Statuses worth another attempt.
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 522, 524, 529})

#: Finish reasons that mean the model declined rather than answered.
REFUSAL_FINISH_REASONS = frozenset({"content_filter", "refusal", "safety"})


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


class OpenAICompatibleProvider(Provider):
    """Chat Completions client built on ``urllib.request``."""

    name = "openai"

    def __init__(self, config) -> None:
        self._config = config
        self._url = config.upstream_base_url.rstrip("/") + "/chat/completions"
        self._opener = urllib.request.build_opener()

    # -- payload ---------------------------------------------------------

    def build_payload(
        self, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Build the upstream request body.

        Every provider-specific field is added deliberately here, so a reader can see the
        whole extension surface in one place.
        """
        config = self._config
        payload: dict[str, Any] = {
            "model": config.upstream_model,
            "messages": messages,
            "max_tokens": config.max_output_tokens,
        }
        if config.temperature is not None:
            payload["temperature"] = config.temperature

        if config.response_format == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "jevmulator_answer",
                    "schema": schema,
                    "strict": True,
                },
            }
        elif config.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}

        if config.thinking == "disabled":
            payload["thinking"] = {"type": "disabled"}
        elif config.thinking == "enabled":
            payload["thinking"] = {"type": "enabled"}

        return payload

    # -- request ---------------------------------------------------------

    def complete(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        timeout: float,
    ) -> ProviderResult:
        config = self._config
        if not config.upstream_api_key:
            raise ProviderRequestError(
                "No upstream API key is available. Set "
                f"{config.upstream_api_key_env} in the environment, or set "
                "JEVMULATOR_UPSTREAM_API_KEY.",
                status=None,
            )

        payload = self.build_payload(messages, schema)
        self.record(
            UpstreamCall(
                model=config.upstream_model,
                messages=[dict(message) for message in messages],
                response_format=config.response_format,
                schema=schema if config.response_format == "json_schema" else None,
            )
        )

        request = urllib.request.Request(
            self._url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": "Bearer " + config.upstream_api_key,
                "User-Agent": "jevmulator/0.1.0",
            },
            method="POST",
        )

        try:
            with self._opener.open(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:1000]
            retry_after = _parse_retry_after(exc.headers.get("retry-after"))
            if retry_after is None:
                milliseconds = _parse_retry_after(exc.headers.get("retry-after-ms"))
                retry_after = milliseconds / 1000.0 if milliseconds is not None else None
            if exc.code in RETRYABLE_STATUSES:
                raise ProviderTransportError(
                    f"Upstream returned HTTP {exc.code}: {detail}",
                    status=exc.code,
                    retry_after_seconds=retry_after,
                ) from exc
            raise ProviderRequestError(
                f"Upstream returned HTTP {exc.code}: {detail}", status=exc.code
            ) from exc
        except socket.timeout as exc:
            raise ProviderTransportError(
                f"Upstream call timed out after {timeout} seconds", timed_out=True
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                raise ProviderTransportError(
                    f"Upstream call timed out after {timeout} seconds", timed_out=True
                ) from exc
            raise ProviderTransportError(f"Upstream connection failed: {exc.reason}") from exc
        except OSError as exc:
            raise ProviderTransportError(f"Upstream connection failed: {exc}") from exc

        return self._parse_response(body)

    def _parse_response(self, body: str) -> ProviderResult:
        try:
            decoded = json.loads(body)
        except ValueError as exc:
            raise ProviderTransportError(
                "Upstream response was not valid JSON"
            ) from exc

        if not isinstance(decoded, dict):
            raise ProviderTransportError("Upstream response was not a JSON object")

        choices = decoded.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderTransportError("Upstream response carried no choices")

        first = choices[0]
        if not isinstance(first, dict):
            raise ProviderTransportError("Upstream choice was not an object")

        finish_reason = first.get("finish_reason")
        message = first.get("message")
        if not isinstance(message, dict):
            raise ProviderTransportError("Upstream choice carried no message")

        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            raise ProviderRefusalError(refusal.strip()[:500])
        if isinstance(finish_reason, str) and finish_reason in REFUSAL_FINISH_REASONS:
            raise ProviderRefusalError(f"Upstream finish_reason was {finish_reason}")

        content = message.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            # Some providers return a content part list.
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict)
                )
            else:
                raise ProviderTransportError("Upstream message content was not text")

        usage = decoded.get("usage")
        input_tokens: int | None = None
        output_tokens: int | None = None
        if isinstance(usage, dict):
            raw_input = usage.get("prompt_tokens", usage.get("input_tokens"))
            raw_output = usage.get("completion_tokens", usage.get("output_tokens"))
            if isinstance(raw_input, int) and not isinstance(raw_input, bool):
                input_tokens = raw_input
            if isinstance(raw_output, int) and not isinstance(raw_output, bool):
                output_tokens = raw_output

        return ProviderResult(
            text=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            raw_model=decoded.get("model") if isinstance(decoded.get("model"), str) else None,
        )

    def close(self) -> None:
        self._opener.close()
