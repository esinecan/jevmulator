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

from . import answers, errors, primitives, prompts, wire
from .answers import AnswerRejected
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


def close_open_brackets(text: str) -> str:
    """Append the closing brackets a JSON text leaves open at its end.

    z.ai's structured-output modes on glm-5.3-flash end a choice answer with 20 or more
    options one ``}`` short, with ``finish_reason`` ``stop``. Only brackets still open at the
    end are added. Text that ends inside a string, or closes a bracket it never opened, is
    returned unchanged, so ``json.loads`` still rejects it.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if not stack or stack.pop() != char:
                return text
    if in_string or not stack:
        return text
    return text + "".join(reversed(stack))


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
        return answers.build_answer(
            question,
            answer,
            normalize=self._config.normalize_probabilities,
            tolerance=self._config.probability_tolerance,
        )

    def _decode(self, text: str) -> dict[str, Any]:
        cleaned = strip_code_fence(text).strip()
        if not cleaned:
            raise AnswerRejected("the upstream answer was empty")
        try:
            payload = json.loads(cleaned)
        except ValueError as exc:
            closed = close_open_brackets(cleaned)
            if closed == cleaned:
                raise AnswerRejected(f"the upstream answer was not valid JSON ({exc})") from exc
            try:
                payload = json.loads(closed)
            except ValueError:
                raise AnswerRejected(f"the upstream answer was not valid JSON ({exc})") from exc
        if not isinstance(payload, dict):
            raise AnswerRejected("the upstream answer was not a JSON object")
        return payload

    # -- error mapping ---------------------------------------------------

    @staticmethod
    def _to_http_error(exc: BaseException) -> errors.JevmulatorError:
        if isinstance(exc, errors.JevmulatorError):
            return exc
        if isinstance(exc, primitives.DistributionError):
            return errors.UpstreamInvalidOutputError(str(exc))
        return errors.UpstreamError(f"The evaluation failed: {exc}")
