"""Read a harness's JSON-lines event file.

The harness writes its events to a file, never to a pipe. The daemon reads new lines from
that file and decides everything from them and from the process handle, so a grandchild
that inherits an output handle can never hang the daemon on end-of-file.

The event shapes are pi's ``--mode json`` shapes. The fake harness writes the same shapes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class EventDigest:
    """What one event tells the supervisor."""

    kind: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    is_model_call: bool = False
    stop_reason: str | None = None
    failure: str | None = None
    settled: bool = False


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return int(value)


def digest(event: dict[str, Any]) -> EventDigest:
    kind = str(event.get("type", ""))
    result = EventDigest(kind=kind)

    if kind == "agent_settled":
        result.settled = True
        return result

    if kind == "extension_error":
        detail = event.get("error") or event.get("message") or event
        result.failure = "extension error: " + (
            detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)[:500]
        )
        return result

    if kind == "message_end":
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return result
        result.is_model_call = True
        provider, model = message.get("provider"), message.get("model")
        if isinstance(provider, str) and isinstance(model, str):
            result.model = f"{provider}/{model}"
        result.stop_reason = message.get("stopReason")
        if result.stop_reason in ("error", "aborted"):
            result.failure = f"model call ended with {result.stop_reason}: " + str(
                message.get("errorMessage") or "no message"
            )
        usage = message.get("usage")
        if isinstance(usage, dict):
            parts = [_count(usage.get(key)) for key in ("input", "cacheRead", "cacheWrite")]
            output = _count(usage.get("output"))
            if parts[0] is not None and output is not None:
                total_input = sum(part or 0 for part in parts)
                # A real model call always has input. A usage block of zeros means the
                # provider reported nothing, and a zero is never passed off as a count.
                if total_input > 0 or output > 0:
                    result.input_tokens = total_input
                    result.output_tokens = output
    return result


class EventTail:
    """Reads the lines appended to an event file since the last read."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._offset = 0
        self._partial = b""

    def read_new(self, *, final: bool = False) -> list[dict[str, Any]]:
        """Complete new events. With ``final``, a last line without a newline counts too."""
        try:
            with open(self._path, "rb") as handle:
                handle.seek(self._offset)
                data = handle.read()
        except FileNotFoundError:
            return []
        self._offset += len(data)
        buffer = self._partial + data
        lines = buffer.split(b"\n")
        self._partial = lines.pop()
        if final and self._partial:
            lines.append(self._partial)
            self._partial = b""
        events: list[dict[str, Any]] = []
        for raw in lines:
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except ValueError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events
