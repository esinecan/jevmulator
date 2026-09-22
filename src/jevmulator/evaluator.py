"""Question evaluation: isolation, bounded retries, and answer assembly.

One request becomes one upstream call per question. The calls run on a shared bounded
thread pool, under one whole-request deadline. A question that cannot produce a valid
answer fails the request; it never becomes an invented judgment.
"""

from __future__ import annotations

import json
import math
import random
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from . import errors, primitives, prompts, wire
from .providers.base import (
    Provider,
    ProviderRefusalError,
    ProviderRequestError,
    ProviderResult,
    ProviderTransportError,
)

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def strip_code_fence(text: str) -> str:
    """Remove a Markdown code fence a prompted model may wrap around its JSON."""
    match = _FENCE.match(text)
    if match:
        return match.group(1)
    return text


class AnswerRejected(Exception):
    """The upstream answer cannot be turned into a valid wire answer."""


@dataclass
class UsageTally:
    """Upstream token counts for one request.

    ``complete`` is false when at least one upstream call reported no usage. Nothing is
    ever invented to fill the gap.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    calls_without_usage: int = 0

    @property
    def complete(self) -> bool:
        return self.calls_without_usage == 0

    def add(self, result: ProviderResult) -> None:
        self.calls += 1
        if result.input_tokens is None and result.output_tokens is None:
            self.calls_without_usage += 1
            return
        if result.input_tokens is None or result.output_tokens is None:
            self.calls_without_usage += 1
        self.input_tokens += result.input_tokens or 0
        self.output_tokens += result.output_tokens or 0


@dataclass
class EvaluationResult:
    answers: dict[str, dict[str, Any]]
    usage: UsageTally
    upstream_attempts: int = 0
    per_question_seconds: dict[str, float] = field(default_factory=dict)


class Evaluator:
    """Evaluates a validated :class:`wire.SystemOneRequest`."""

    def __init__(self, config, provider: Provider, executor: ThreadPoolExecutor) -> None:
        self._config = config
        self._provider = provider
        self._executor = executor
        self._lock = threading.Lock()
        self._random = random.Random(0x4A4556)

    # -- public ----------------------------------------------------------

    def evaluate(self, request: wire.SystemOneRequest) -> EvaluationResult:
        """Evaluate every question in ``request`` independently.

        Raises:
            errors.JevmulatorError: The request cannot be answered. Nothing partial is
                returned, because the pinned response requires an answer for every
                question.
        """
        config = self._config
        deadline = time.monotonic() + config.request_timeout_seconds
        usage = UsageTally()
        attempts_total = 0
        timings: dict[str, float] = {}

        futures: dict[str, Future] = {}
        for question_id, question in request.questions.items():
            futures[question_id] = self._executor.submit(
                self._evaluate_one, request.state, question, deadline
            )

        answers: dict[str, dict[str, Any]] = {}
        failures: dict[str, BaseException] = {}
        try:
            for question_id, future in futures.items():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failures[question_id] = errors.UpstreamTimeoutError(
                        "The request exceeded "
                        f"{config.request_timeout_seconds} seconds before every question "
                        "was answered."
                    )
                    continue
                try:
                    answer, question_usage, attempts, elapsed = future.result(timeout=remaining)
                except TimeoutError:
                    failures[question_id] = errors.UpstreamTimeoutError(
                        "The request exceeded "
                        f"{config.request_timeout_seconds} seconds before every question "
                        "was answered."
                    )
                except BaseException as exc:  # noqa: BLE001 - re-raised below in order
                    failures[question_id] = exc
                else:
                    answers[question_id] = answer
                    attempts_total += attempts
                    timings[question_id] = elapsed
                    usage.input_tokens += question_usage.input_tokens
                    usage.output_tokens += question_usage.output_tokens
                    usage.calls += question_usage.calls
                    usage.calls_without_usage += question_usage.calls_without_usage
        finally:
            for question_id, future in futures.items():
                if question_id in failures or not future.done():
                    future.cancel()

        if failures:
            for question_id in request.question_ids:
                if question_id in failures:
                    raise self._to_http_error(failures[question_id])

        if config.usage_policy == "strict" and not usage.complete:
            raise errors.UsageUnavailableError(
                "The upstream provider reported no token counts for "
                f"{usage.calls_without_usage} of {usage.calls} calls, and "
                "JEVMULATOR_USAGE_POLICY is strict."
            )

        return EvaluationResult(
            answers=answers,
            usage=usage,
            upstream_attempts=attempts_total,
            per_question_seconds=timings,
        )

    # -- one question ----------------------------------------------------

    def _evaluate_one(
        self, state: Any, question: wire.Question, deadline: float
    ) -> tuple[dict[str, Any], UsageTally, int, float]:
        config = self._config
        started = time.monotonic()
        usage = UsageTally()
        attempts = 0

        schema = prompts.output_schema(question)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": prompts.system_prompt(question)},
            {"role": "user", "content": prompts.user_prompt(state, question)},
        ]
        if config.response_format == "none":
            messages[0]["content"] += "\n\n" + prompts.schema_instruction(schema)

        last_rejection = ""
        for repair_index in range(config.repair_retries + 1):
            result = self._complete_with_retries(messages, schema, deadline)
            attempts += 1
            usage.add(result)
            try:
                answer = self._build_answer(question, result.text)
            except AnswerRejected as exc:
                last_rejection = str(exc)
                if repair_index >= config.repair_retries:
                    break
                messages = messages + [
                    {"role": "assistant", "content": result.text[:4000]},
                    {"role": "user", "content": prompts.repair_instruction(last_rejection)},
                ]
                continue
            return answer, usage, attempts, time.monotonic() - started

        raise errors.UpstreamInvalidOutputError(
            "The upstream model did not return a usable answer after "
            f"{config.repair_retries + 1} attempts: {last_rejection}"
        )

    def _complete_with_retries(
        self, messages: list[dict[str, str]], schema: dict[str, Any], deadline: float
    ) -> ProviderResult:
        config = self._config
        last: ProviderTransportError | None = None

        for attempt in range(config.upstream_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise errors.UpstreamTimeoutError(
                    f"The request exceeded {config.request_timeout_seconds} seconds."
                )
            timeout = min(config.upstream_timeout_seconds, remaining)
            try:
                return self._provider.complete(messages, schema, timeout=timeout)
            except ProviderRefusalError as exc:
                raise errors.UpstreamRefusalError(
                    f"The upstream model refused to answer: {exc}"
                ) from exc
            except ProviderRequestError as exc:
                if exc.status is None:
                    raise errors.UpstreamNotConfiguredError(str(exc)) from exc
                raise errors.UpstreamError(
                    f"The upstream provider rejected the request: {exc.message}"
                ) from exc
            except ProviderTransportError as exc:
                last = exc
                if attempt >= config.upstream_retries:
                    break
                self._sleep_before_retry(attempt, exc, deadline)

        assert last is not None
        return self._raise_transport(last)

    def _sleep_before_retry(
        self, attempt: int, error: ProviderTransportError, deadline: float
    ) -> None:
        config = self._config
        backoff = min(
            config.retry_backoff_seconds * (2**attempt), config.retry_backoff_max_seconds
        )
        if error.retry_after_seconds is not None:
            backoff = max(backoff, error.retry_after_seconds)
        with self._lock:
            jitter = self._random.random() * config.retry_backoff_seconds
        remaining = deadline - time.monotonic()
        delay = max(0.0, min(backoff + jitter, remaining))
        if delay > 0:
            time.sleep(delay)

    def _raise_transport(self, error: ProviderTransportError):
        if error.timed_out:
            raise errors.UpstreamTimeoutError(
                f"The upstream provider did not answer: {error.message}"
            )
        if error.status == 429:
            headers = {}
            if error.retry_after_seconds is not None:
                headers["retry-after"] = str(int(math.ceil(error.retry_after_seconds)))
            raise errors.RateLimitedError(
                f"The upstream provider rate limited this daemon: {error.message}",
                headers=headers,
            )
        if error.status in (503, 529):
            raise errors.OverloadedError(
                f"The upstream provider is overloaded: {error.message}"
            )
        raise errors.UpstreamError(f"The upstream provider failed: {error.message}")

    # -- answer construction ---------------------------------------------

    def _build_answer(self, question: wire.Question, text: str) -> dict[str, Any]:
        payload = self._decode(text)
        answer = payload.get(prompts.ANSWER_KEY)
        if not isinstance(answer, dict):
            raise AnswerRejected(
                f"the JSON object had no object under the key {prompts.ANSWER_KEY!r}"
            )

        if isinstance(question, wire.NoulQuestion):
            return self._build_noul(answer)
        if isinstance(question, wire.ChoiceQuestion):
            return self._build_choice(question, answer)
        return self._build_score(question, answer)

    def _decode(self, text: str) -> dict[str, Any]:
        cleaned = strip_code_fence(text).strip()
        if not cleaned:
            raise AnswerRejected("the upstream answer was empty")
        try:
            payload = json.loads(cleaned)
        except ValueError as exc:
            raise AnswerRejected(f"the upstream answer was not valid JSON ({exc})") from exc
        if not isinstance(payload, dict):
            raise AnswerRejected("the upstream answer was not a JSON object")
        return payload

    def _build_noul(self, answer: dict[str, Any]) -> dict[str, Any]:
        if "p_yes" not in answer:
            raise AnswerRejected("the answer object had no 'p_yes' key")
        value = answer["p_yes"]
        if not primitives.is_finite_number(value):
            raise AnswerRejected("'p_yes' was not a finite number")
        value = float(value)
        if value < 0.0 or value > 1.0:
            raise AnswerRejected(f"'p_yes' was {value}, outside the range 0 to 1")
        return wire.noul_answer(value)

    def _extract_distribution(
        self, answer: dict[str, Any], expected_keys: list[str]
    ) -> list[float]:
        raw = answer.get("probabilities")
        if not isinstance(raw, dict):
            raise AnswerRejected("the answer object had no 'probabilities' object")
        unknown = [key for key in raw if key not in expected_keys]
        if unknown:
            raise AnswerRejected(
                "the answer offered candidates that were never requested: "
                + ", ".join(repr(key) for key in sorted(unknown)[:5])
            )
        missing = [key for key in expected_keys if key not in raw]
        if missing:
            raise AnswerRejected(
                "the answer left out requested candidates: "
                + ", ".join(repr(key) for key in missing[:5])
            )
        values = [raw[key] for key in expected_keys]
        try:
            primitives.check_probabilities(values, where="probabilities")
        except primitives.DistributionError as exc:
            raise AnswerRejected(str(exc)) from exc
        return [float(value) for value in values]

    def _build_choice(
        self, question: wire.ChoiceQuestion, answer: dict[str, Any]
    ) -> dict[str, Any]:
        labels = question.labels
        values = self._extract_distribution(answer, labels)
        emitted, _error, _rescaled = primitives.normalize_if_needed(
            values,
            enabled=self._config.normalize_probabilities,
            tolerance=self._config.probability_tolerance,
        )
        selected = primitives.argmax_label(labels, emitted)
        confidence = primitives.choice_confidence(emitted)
        return wire.choice_answer(
            selected, confidence, dict(zip(labels, emitted))
        )

    def _build_score(
        self, question: wire.ScoreQuestion, answer: dict[str, Any]
    ) -> dict[str, Any]:
        keys = question.level_keys
        values = self._extract_distribution(answer, keys)
        emitted, _error, _rescaled = primitives.normalize_if_needed(
            values,
            enabled=self._config.normalize_probabilities,
            tolerance=self._config.probability_tolerance,
        )
        score = primitives.expected_score(emitted)
        confidence = primitives.score_confidence(emitted)
        return wire.score_answer(
            score, confidence, question.legend(), dict(zip(keys, emitted))
        )

    # -- error mapping ---------------------------------------------------

    @staticmethod
    def _to_http_error(exc: BaseException) -> errors.JevmulatorError:
        if isinstance(exc, errors.JevmulatorError):
            return exc
        if isinstance(exc, primitives.DistributionError):
            return errors.UpstreamInvalidOutputError(str(exc))
        return errors.UpstreamError(f"The evaluation failed: {exc}")
