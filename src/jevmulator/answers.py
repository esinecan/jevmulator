"""Turn one decoded answer object into one pinned wire answer.

These checks were private methods of ``Evaluator``. They live here so that two callers
apply the same rules: the bare evaluator, to an upstream model's answer, and the sys1 form,
to an agent's submission. The daemon computes every derived field itself; an answer object
carries only a distribution.
"""

from __future__ import annotations

import math
from typing import Any

from . import primitives, wire

#: Added to a sum-error limit before it is compared, so a limit of 0.01 accepts
#: ``[0.33, 0.33, 0.33]``, whose floating-point error is 0.010000000000000009.
SUM_ERROR_MARGIN = 1e-9


class AnswerRejected(Exception):
    """An answer object cannot become a wire answer. The message says why."""


def build_noul(answer: dict[str, Any]) -> dict[str, Any]:
    if "p_yes" not in answer:
        raise AnswerRejected("the answer object had no 'p_yes' key")
    value = answer["p_yes"]
    if not primitives.is_finite_number(value):
        raise AnswerRejected("'p_yes' was not a finite number")
    value = float(value)
    if value < 0.0 or value > 1.0:
        raise AnswerRejected(f"'p_yes' was {value}, outside the range 0 to 1")
    return wire.noul_answer(value)


def extract_distribution(answer: dict[str, Any], expected_keys: list[str]) -> list[float]:
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


def check_sum(values: list[float], max_sum_error: float | None) -> None:
    """Reject a distribution whose sum is further from one than ``max_sum_error``.

    ``None`` switches the check off. The bare path passes ``None``: it rescales any sum
    and never rejects on the sum alone.
    """
    if max_sum_error is None:
        return
    total = math.fsum(values)
    if abs(total - 1.0) > max_sum_error + SUM_ERROR_MARGIN:
        raise AnswerRejected(
            f"the probabilities sum to {round(total, 6)}; they must sum to 1 "
            f"within {max_sum_error}"
        )


def build_choice(
    question: wire.ChoiceQuestion,
    answer: dict[str, Any],
    *,
    normalize: bool,
    tolerance: float,
    max_sum_error: float | None = None,
) -> dict[str, Any]:
    labels = question.labels
    values = extract_distribution(answer, labels)
    check_sum(values, max_sum_error)
    emitted, _error, _rescaled = primitives.normalize_if_needed(
        values, enabled=normalize, tolerance=tolerance
    )
    selected = primitives.argmax_label(labels, emitted)
    confidence = primitives.choice_confidence(emitted)
    return wire.choice_answer(selected, confidence, dict(zip(labels, emitted)))


def build_score(
    question: wire.ScoreQuestion,
    answer: dict[str, Any],
    *,
    normalize: bool,
    tolerance: float,
    max_sum_error: float | None = None,
) -> dict[str, Any]:
    keys = question.level_keys
    values = extract_distribution(answer, keys)
    check_sum(values, max_sum_error)
    emitted, _error, _rescaled = primitives.normalize_if_needed(
        values, enabled=normalize, tolerance=tolerance
    )
    score = primitives.expected_score(emitted)
    confidence = primitives.score_confidence(emitted)
    return wire.score_answer(score, confidence, question.legend(), dict(zip(keys, emitted)))


def build_answer(
    question: wire.Question,
    answer: dict[str, Any],
    *,
    normalize: bool,
    tolerance: float,
    max_sum_error: float | None = None,
) -> dict[str, Any]:
    """Validate ``answer`` for ``question`` and build the wire answer.

    Raises:
        AnswerRejected: The first problem found. The message is written so a model or an
            agent can act on it.
    """
    if isinstance(question, wire.NoulQuestion):
        return build_noul(answer)
    if isinstance(question, wire.ChoiceQuestion):
        return build_choice(
            question, answer, normalize=normalize, tolerance=tolerance,
            max_sum_error=max_sum_error,
        )
    return build_score(
        question, answer, normalize=normalize, tolerance=tolerance,
        max_sum_error=max_sum_error,
    )
