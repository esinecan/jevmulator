"""Distribution arithmetic for the three Jev primitives.

The two confidence functions reproduce the algorithms published in the official TypeSafe
adapter ``typesafe-ai/system-one-adapter-python`` at commit
``e1d4cc938204b22fc5a3c3aca7044072fe3f712d``, file
``src/system_one_adapter/_utils/confidence_metrics.py``.

They are a **documented compatibility policy**. They are verified adapter algorithms. They
are not verified TypeSafe production algorithms: the confidence documentation calls its own
worked example an approximation, and some official example numbers do not equal these
formulas exactly. See docs/compatibility.md.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

#: Sum tolerance published by the official adapter's probability normalization module.
PROBABILITY_TOLERANCE = 1e-6


class DistributionError(ValueError):
    """A distribution cannot be used as a probability distribution."""


def is_finite_number(value: object) -> bool:
    """True when ``value`` is a real number that is neither NaN nor infinite.

    ``bool`` is excluded: ``True`` is an ``int`` in Python, but a boolean where a
    probability is required is a malformed answer.
    """
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def check_probabilities(values: Sequence[float], *, where: str) -> None:
    """Reject a distribution that cannot carry meaning.

    Raises:
        DistributionError: A value is not finite, a value is outside ``[0, 1]``, the
            sequence is empty, or every value is zero.
    """
    if not values:
        raise DistributionError(f"{where}: distribution is empty")
    for index, value in enumerate(values):
        if not is_finite_number(value):
            raise DistributionError(f"{where}: value at position {index} is not a finite number")
        if value < 0.0 or value > 1.0:
            raise DistributionError(
                f"{where}: value at position {index} is {value}, outside the range 0 to 1"
            )
    total = math.fsum(float(value) for value in values)
    if total <= 0.0:
        raise DistributionError(
            f"{where}: the probabilities sum to {total}, so no outcome was given any weight"
        )


def sum_error(values: Iterable[float]) -> float:
    """Absolute distance between the sum of ``values`` and one."""
    return abs(math.fsum(float(value) for value in values) - 1.0)


def rescale(values: Sequence[float]) -> list[float]:
    """Rescale a positive-sum distribution so that it sums to one.

    Unlike the official adapter, a zero total is not replaced by a uniform distribution.
    A zero total from a language model is a failed answer, and :func:`check_probabilities`
    rejects it before this function runs.
    """
    total = math.fsum(float(value) for value in values)
    if total <= 0.0:
        raise DistributionError("cannot rescale a distribution whose total is not positive")
    return [float(value) / total for value in values]


def normalize_if_needed(
    values: Sequence[float], *, enabled: bool, tolerance: float = PROBABILITY_TOLERANCE
) -> tuple[list[float], float, bool]:
    """Rescale when the sum is off by more than ``tolerance``.

    Returns:
        The distribution to emit, the original sum error, and whether a rescale happened.
    """
    error = sum_error(values)
    if not enabled or error <= tolerance:
        return [float(value) for value in values], error, False
    return rescale(values), error, True


def choice_confidence(probabilities: Sequence[float]) -> float:
    """Scale the peak probability from uniform to certainty.

    ``(max(p) - 1/N) / (1 - 1/N)``, and ``1.0`` when there is one option.
    Official adapter algorithm; not a verified TypeSafe production algorithm.
    """
    count = len(probabilities)
    if count == 0:
        raise DistributionError("choice confidence needs at least one option")
    if count == 1:
        return 1.0
    normalized = rescale(probabilities)
    uniform = 1.0 / count
    return (max(normalized) - uniform) / (1.0 - uniform)


def score_confidence(probabilities: Sequence[float]) -> float:
    """Measure how tightly the distribution sits around its modal level.

    First modal index ``m``; ``D = sum(p_i * |i - m|)``; ``U = mean(|i - (N-1)/2|)``;
    result ``max(0, 1 - D/U)``, and ``1.0`` when there is one level.
    Official adapter algorithm; not a verified TypeSafe production algorithm.
    """
    count = len(probabilities)
    if count == 0:
        raise DistributionError("score confidence needs at least one level")
    if count == 1:
        return 1.0
    normalized = rescale(probabilities)
    mode_index = max(range(count), key=normalized.__getitem__)
    distance_from_mode = math.fsum(
        probability * abs(index - mode_index) for index, probability in enumerate(normalized)
    )
    uniform_center = (count - 1) / 2
    uniform_mean_absolute_deviation = (
        math.fsum(abs(index - uniform_center) for index in range(count)) / count
    )
    return max(0.0, 1.0 - distance_from_mode / uniform_mean_absolute_deviation)


def expected_score(probabilities: Sequence[float]) -> float:
    """Probability-weighted average of the zero-based level indices.

    The distribution is rescaled first, so the expected value is taken over a true
    distribution regardless of whether the emitted probabilities were normalized.
    """
    normalized = rescale(probabilities)
    return math.fsum(index * probability for index, probability in enumerate(normalized))


def argmax_label(labels: Sequence[str], probabilities: Sequence[float]) -> str:
    """The label with the highest probability.

    A tie resolves to the label that appeared earliest in the request's criteria order.
    TypeSafe's tie rule is undocumented, so this is a Jevmulator policy.
    """
    if len(labels) != len(probabilities):
        raise DistributionError("labels and probabilities must have the same length")
    if not labels:
        raise DistributionError("argmax needs at least one label")
    best_index = 0
    best_value = float(probabilities[0])
    for index in range(1, len(labels)):
        value = float(probabilities[index])
        if value > best_value:
            best_index = index
            best_value = value
    return labels[best_index]
