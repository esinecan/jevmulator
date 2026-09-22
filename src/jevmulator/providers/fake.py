"""Deterministic offline provider.

Its answer depends only on the bytes of the messages it receives. Two requests that send
the same upstream payload therefore get the same answer, which is what makes the
metamorphic isolation tests meaningful: an equal answer after renaming a question ID means
the payload was equal, and the recorded payloads prove it directly.

``JEVMULATOR_FAKE_MODE`` injects a specific malformed or failing answer, so the evaluator's
failure paths can be exercised without a network.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from .base import (
    Provider,
    ProviderRefusalError,
    ProviderResult,
    ProviderTransportError,
    UpstreamCall,
)

FAKE_MODES = (
    "deterministic",
    "invalid_json",
    "missing_answer_key",
    "unknown_candidate",
    "missing_candidate",
    "nonfinite",
    "out_of_range",
    "zero_total",
    "unnormalized",
    "refusal",
    "no_usage",
    "transport_error",
    "timeout",
    "slow",
)


def _digest_floats(seed: bytes, count: int) -> list[float]:
    """Produce ``count`` positive weights from ``seed`` deterministically."""
    weights: list[float] = []
    block = seed
    while len(weights) < count:
        block = hashlib.sha256(block).digest()
        for index in range(0, len(block), 4):
            if len(weights) >= count:
                break
            chunk = int.from_bytes(block[index : index + 4], "big")
            weights.append((chunk % 10_000) / 10_000.0 + 0.01)
    return weights


class FakeProvider(Provider):
    """Offline provider used by the default test suites and by ``JEVMULATOR_PROVIDER=fake``."""

    name = "fake"

    def __init__(self, config) -> None:
        self._config = config
        self._mode = os.environ.get("JEVMULATOR_FAKE_MODE", "deterministic")
        if self._mode not in FAKE_MODES:
            self._mode = "deterministic"
        self._delay = float(os.environ.get("JEVMULATOR_FAKE_DELAY_SECONDS", "0") or 0)
        self._attempts = 0

    @property
    def attempts(self) -> int:
        """How many completions this provider was asked for."""
        return self._attempts

    def complete(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        timeout: float,
    ) -> ProviderResult:
        self._attempts += 1
        self.record(
            UpstreamCall(
                model=self._config.upstream_model,
                messages=[dict(message) for message in messages],
                response_format=self._config.response_format,
                schema=schema,
            )
        )

        if self._delay > 0:
            time.sleep(min(self._delay, timeout + 1.0))

        mode = self._mode
        if mode == "timeout":
            raise ProviderTransportError("fake upstream timed out", timed_out=True)
        if mode == "transport_error":
            raise ProviderTransportError("fake upstream connection failed", status=503)
        if mode == "refusal":
            raise ProviderRefusalError("fake upstream refused to answer")
        if mode == "invalid_json":
            return ProviderResult(text="this is not json", input_tokens=11, output_tokens=3)
        if mode == "missing_answer_key":
            return ProviderResult(
                text=json.dumps({"result": {"p_yes": 0.5}}), input_tokens=11, output_tokens=3
            )

        answer = self._build_answer(messages, schema, mode)
        text = json.dumps({"answer": answer}, ensure_ascii=False)
        if mode == "no_usage":
            return ProviderResult(text=text)
        return ProviderResult(
            text=text,
            input_tokens=len(json.dumps(messages, ensure_ascii=False)) // 4 + 1,
            output_tokens=len(text) // 4 + 1,
        )

    def _build_answer(
        self, messages: list[dict[str, str]], schema: dict[str, Any], mode: str
    ) -> dict[str, Any]:
        seed = json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
        inner = schema["properties"]["answer"]
        properties = inner.get("properties", {})

        if "p_yes" in properties:
            value = _digest_floats(seed, 1)[0]
            probability = min(max(value, 0.0), 1.0)
            if mode == "nonfinite":
                return {"p_yes": float("nan")}
            if mode == "out_of_range":
                return {"p_yes": 1.5}
            if mode == "missing_candidate":
                return {}
            return {"p_yes": probability}

        keys = list(properties["probabilities"]["properties"].keys())
        weights = _digest_floats(seed, len(keys))
        total = sum(weights)
        probabilities = {key: weight / total for key, weight in zip(keys, weights)}

        if mode == "nonfinite":
            probabilities[keys[0]] = float("inf")
        elif mode == "out_of_range":
            probabilities[keys[0]] = -0.2
        elif mode == "zero_total":
            probabilities = dict.fromkeys(keys, 0.0)
        elif mode == "unnormalized":
            probabilities = {key: value * 3.0 for key, value in probabilities.items()}
        elif mode == "unknown_candidate":
            probabilities["a-label-that-was-never-offered"] = 0.1
        elif mode == "missing_candidate" and len(keys) > 1:
            probabilities.pop(keys[-1])

        return {"probabilities": probabilities}

    def close(self) -> None:
        return None
