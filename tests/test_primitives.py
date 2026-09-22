"""Distribution arithmetic: the two confidence formulas, expected score, and edges."""

from __future__ import annotations

import math

import pytest

from jevmulator import primitives


class TestIsFiniteNumber:
    @pytest.mark.parametrize("value", [0, 1, 0.5, -3, 1e300, 0.0])
    def test_accepts_real_numbers(self, value: object) -> None:
        assert primitives.is_finite_number(value) is True

    @pytest.mark.parametrize(
        "value",
        [True, False, None, "0.5", [], {}, float("nan"), float("inf"), float("-inf")],
    )
    def test_rejects_everything_else(self, value: object) -> None:
        assert primitives.is_finite_number(value) is False


class TestCheckProbabilities:
    def test_accepts_a_normal_distribution(self) -> None:
        primitives.check_probabilities([0.2, 0.3, 0.5], where="test")

    def test_accepts_an_unnormalised_but_positive_distribution(self) -> None:
        primitives.check_probabilities([0.2, 0.2, 0.2], where="test")

    def test_rejects_an_empty_distribution(self) -> None:
        with pytest.raises(primitives.DistributionError, match="empty"):
            primitives.check_probabilities([], where="test")

    def test_rejects_a_zero_total(self) -> None:
        with pytest.raises(primitives.DistributionError, match="no outcome was given any weight"):
            primitives.check_probabilities([0.0, 0.0], where="test")

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_a_nonfinite_value(self, bad: float) -> None:
        with pytest.raises(primitives.DistributionError, match="not a finite number"):
            primitives.check_probabilities([0.5, bad], where="test")

    @pytest.mark.parametrize("bad", [-0.001, 1.001, 2.0])
    def test_rejects_a_value_outside_zero_to_one(self, bad: float) -> None:
        with pytest.raises(primitives.DistributionError, match="outside the range"):
            primitives.check_probabilities([0.5, bad], where="test")

    def test_rejects_a_boolean(self) -> None:
        with pytest.raises(primitives.DistributionError, match="not a finite number"):
            primitives.check_probabilities([True, 0.5], where="test")


class TestRescale:
    def test_scales_to_one(self) -> None:
        result = primitives.rescale([1.0, 1.0, 2.0])
        assert result == pytest.approx([0.25, 0.25, 0.5])
        assert math.fsum(result) == pytest.approx(1.0)

    def test_leaves_a_normalised_distribution_alone(self) -> None:
        assert primitives.rescale([0.25, 0.75]) == pytest.approx([0.25, 0.75])

    def test_refuses_a_zero_total(self) -> None:
        with pytest.raises(primitives.DistributionError):
            primitives.rescale([0.0, 0.0])


class TestNormalizeIfNeeded:
    def test_leaves_a_distribution_inside_the_tolerance_untouched(self) -> None:
        values = [0.5, 0.5 - 1e-9]
        emitted, error, rescaled = primitives.normalize_if_needed(values, enabled=True)
        assert emitted == pytest.approx(values)
        assert error < primitives.PROBABILITY_TOLERANCE
        assert rescaled is False

    def test_rescales_a_distribution_outside_the_tolerance(self) -> None:
        emitted, error, rescaled = primitives.normalize_if_needed([0.6, 0.6], enabled=True)
        assert math.fsum(emitted) == pytest.approx(1.0)
        assert error == pytest.approx(0.2)
        assert rescaled is True

    def test_leaves_the_distribution_alone_when_disabled(self) -> None:
        emitted, error, rescaled = primitives.normalize_if_needed([0.6, 0.6], enabled=False)
        assert emitted == pytest.approx([0.6, 0.6])
        assert error == pytest.approx(0.2)
        assert rescaled is False


class TestChoiceConfidence:
    """Adapter formula: (max(p) - 1/N) / (1 - 1/N)."""

    def test_one_option_is_certain(self) -> None:
        assert primitives.choice_confidence([0.3]) == 1.0

    def test_uniform_is_zero(self) -> None:
        assert primitives.choice_confidence([0.25] * 4) == pytest.approx(0.0)

    def test_certain_is_one(self) -> None:
        assert primitives.choice_confidence([1.0, 0.0, 0.0]) == pytest.approx(1.0)

    def test_matches_the_published_formula(self) -> None:
        probabilities = [0.8, 0.1, 0.1]
        expected = (0.8 - 1 / 3) / (1 - 1 / 3)
        assert primitives.choice_confidence(probabilities) == pytest.approx(expected)

    def test_normalises_before_measuring(self) -> None:
        assert primitives.choice_confidence([1.6, 0.2, 0.2]) == pytest.approx(
            primitives.choice_confidence([0.8, 0.1, 0.1])
        )

    def test_rejects_an_empty_distribution(self) -> None:
        with pytest.raises(primitives.DistributionError):
            primitives.choice_confidence([])


class TestScoreConfidence:
    """Adapter formula: max(0, 1 - D/U) about the first modal level."""

    def test_one_level_is_certain(self) -> None:
        assert primitives.score_confidence([0.4]) == 1.0

    def test_certain_is_one(self) -> None:
        assert primitives.score_confidence([0.0, 1.0, 0.0]) == pytest.approx(1.0)

    def test_matches_the_published_formula(self) -> None:
        probabilities = [0.1, 0.1, 0.8]
        mode = 2
        distance = sum(p * abs(i - mode) for i, p in enumerate(probabilities))
        centre = (3 - 1) / 2
        uniform = sum(abs(i - centre) for i in range(3)) / 3
        assert primitives.score_confidence(probabilities) == pytest.approx(
            max(0.0, 1 - distance / uniform)
        )

    def test_never_returns_a_negative_value(self) -> None:
        # Mass at both ends spreads further than uniform, so the raw value is negative.
        assert primitives.score_confidence([0.5, 0.0, 0.0, 0.0, 0.5]) == 0.0

    def test_uses_the_first_mode_on_a_tie(self) -> None:
        tied = [0.5, 0.0, 0.5]
        distance = sum(p * abs(i - 0) for i, p in enumerate(tied))
        centre = 1.0
        uniform = sum(abs(i - centre) for i in range(3)) / 3
        assert primitives.score_confidence(tied) == pytest.approx(
            max(0.0, 1 - distance / uniform)
        )


class TestExpectedScore:
    def test_a_certain_level_gives_that_level(self) -> None:
        assert primitives.expected_score([0.0, 0.0, 1.0]) == pytest.approx(2.0)

    def test_matches_the_documented_example(self) -> None:
        # Official documentation example: {"0": 0.1, "1": 0.1, "2": 0.8} -> 1.7
        assert primitives.expected_score([0.1, 0.1, 0.8]) == pytest.approx(1.7)

    def test_is_taken_over_a_rescaled_distribution(self) -> None:
        assert primitives.expected_score([0.2, 0.2, 1.6]) == pytest.approx(1.7)

    def test_one_level_is_zero(self) -> None:
        assert primitives.expected_score([0.9]) == pytest.approx(0.0)


class TestArgmaxLabel:
    def test_picks_the_highest(self) -> None:
        assert primitives.argmax_label(["a", "b", "c"], [0.1, 0.7, 0.2]) == "b"

    def test_resolves_a_tie_to_the_earliest_label(self) -> None:
        assert primitives.argmax_label(["a", "b", "c"], [0.5, 0.5, 0.0]) == "a"
        assert primitives.argmax_label(["c", "b", "a"], [0.5, 0.5, 0.0]) == "c"

    def test_rejects_mismatched_lengths(self) -> None:
        with pytest.raises(primitives.DistributionError):
            primitives.argmax_label(["a"], [0.5, 0.5])

    def test_rejects_an_empty_label_list(self) -> None:
        with pytest.raises(primitives.DistributionError):
            primitives.argmax_label([], [])


class TestNumericalEdges:
    def test_tiny_probabilities_still_normalise(self) -> None:
        values = [1e-12, 2e-12, 1e-12]
        emitted, _error, rescaled = primitives.normalize_if_needed(values, enabled=True)
        assert rescaled is True
        assert math.fsum(emitted) == pytest.approx(1.0)
        assert primitives.argmax_label(["a", "b", "c"], emitted) == "b"

    def test_many_levels_stay_bounded(self) -> None:
        count = 64
        values = [1.0 / count] * count
        assert 0.0 <= primitives.score_confidence(values) <= 1.0
        assert primitives.expected_score(values) == pytest.approx((count - 1) / 2)

    def test_confidence_stays_within_zero_and_one(self) -> None:
        for values in ([0.9, 0.05, 0.05], [0.34, 0.33, 0.33], [0.5, 0.5], [1.0, 0.0]):
            assert 0.0 <= primitives.choice_confidence(values) <= 1.0
            assert 0.0 <= primitives.score_confidence(values) <= 1.0
