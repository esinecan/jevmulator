"""Direct tests for the shared answer builders.

The bare evaluator applies these to an upstream model's answer, and the sys1 form applies
them to an agent's submission. Before the builders were shared they were private evaluator
methods, reached only through HTTP.
"""

from __future__ import annotations

import math

import pytest

from jevmulator import answers, wire
from jevmulator.answers import AnswerRejected

CHOICE = wire.ChoiceQuestion(criteria={"angry": "upset", "calm": None, "excited": "eager"})
SCORE = wire.ScoreQuestion(criteria=["Can wait", {"level": "soon"}, ["today", "now"]])
NOUL = wire.NoulQuestion()


def build(question, answer, **kwargs):
    kwargs.setdefault("normalize", True)
    kwargs.setdefault("tolerance", 1e-6)
    return answers.build_answer(question, answer, **kwargs)


class TestNoul:
    def test_valid(self):
        assert build(NOUL, {"p_yes": 0.25}) == {"type": "noul", "noul": 0.25}

    @pytest.mark.parametrize(
        "answer, fragment",
        [
            ({}, "no 'p_yes' key"),
            ({"p_yes": True}, "not a finite number"),
            ({"p_yes": float("nan")}, "not a finite number"),
            ({"p_yes": "0.5"}, "not a finite number"),
            ({"p_yes": 1.5}, "outside the range 0 to 1"),
            ({"p_yes": -0.1}, "outside the range 0 to 1"),
        ],
    )
    def test_rejections(self, answer, fragment):
        with pytest.raises(AnswerRejected, match=fragment):
            build(NOUL, answer)


class TestDistribution:
    def test_unknown_label_is_named(self):
        with pytest.raises(AnswerRejected, match="never requested: 'bored'"):
            build(CHOICE, {"probabilities": {"angry": 0.5, "calm": 0.5, "excited": 0.0, "bored": 0.0}})

    def test_missing_label_is_named(self):
        with pytest.raises(AnswerRejected, match="left out requested candidates: 'excited'"):
            build(CHOICE, {"probabilities": {"angry": 0.5, "calm": 0.5}})

    def test_zero_total_is_rejected_not_made_uniform(self):
        with pytest.raises(AnswerRejected, match="no outcome was given any weight"):
            build(CHOICE, {"probabilities": {"angry": 0, "calm": 0, "excited": 0}})

    def test_boolean_value_is_rejected(self):
        with pytest.raises(AnswerRejected, match="not a finite number"):
            build(CHOICE, {"probabilities": {"angry": True, "calm": 0, "excited": 0}})

    def test_missing_probabilities_object(self):
        with pytest.raises(AnswerRejected, match="no 'probabilities' object"):
            build(CHOICE, {"distribution": {}})


class TestChoice:
    def test_derived_fields(self):
        result = build(CHOICE, {"probabilities": {"angry": 0.1, "calm": 0.7, "excited": 0.2}})
        assert result["choice"] == "calm"
        assert result["confidence"] == pytest.approx((0.7 - 1 / 3) / (1 - 1 / 3))
        assert list(result["probabilities"]) == ["angry", "calm", "excited"]

    def test_tie_goes_to_the_earliest_label(self):
        result = build(CHOICE, {"probabilities": {"excited": 0.4, "calm": 0.2, "angry": 0.4}})
        assert result["choice"] == "angry"

    def test_off_sum_is_rescaled_when_normalising(self):
        result = build(CHOICE, {"probabilities": {"angry": 0.2, "calm": 0.4, "excited": 0.4}}, normalize=True)
        assert math.fsum(result["probabilities"].values()) == pytest.approx(1.0)
        result = build(CHOICE, {"probabilities": {"angry": 0.1, "calm": 0.2, "excited": 0.2}}, normalize=True)
        assert result["probabilities"]["calm"] == pytest.approx(0.4)

    def test_off_sum_is_kept_when_not_normalising(self):
        result = build(CHOICE, {"probabilities": {"angry": 0.1, "calm": 0.2, "excited": 0.2}}, normalize=False)
        assert result["probabilities"]["calm"] == pytest.approx(0.2)


class TestScore:
    def test_expected_score_and_structured_legend(self):
        result = build(SCORE, {"probabilities": {"0": 0.2, "1": 0.5, "2": 0.3}})
        assert result["score"] == pytest.approx(1.1)
        assert result["legend"] == {"0": "Can wait", "1": {"level": "soon"}, "2": ["today", "now"]}


class TestSumLimit:
    def test_no_limit_by_default(self):
        result = build(CHOICE, {"probabilities": {"angry": 0.1, "calm": 0.1, "excited": 0.1}})
        assert result["choice"] == "angry"

    def test_limit_rejects_a_far_sum_and_names_it(self):
        with pytest.raises(AnswerRejected, match=r"sum to 0\.9; they must sum to 1 within 0\.01"):
            build(CHOICE, {"probabilities": {"angry": 0.3, "calm": 0.3, "excited": 0.3}}, max_sum_error=0.01)

    def test_limit_accepts_thirds_written_as_0_33(self):
        # fsum([0.33, 0.33, 0.33]) is 0.99, and 1 - 0.99 is 0.010000000000000009 in floating
        # point. A strict comparison against 0.01 would reject an answer an agent writes often.
        values = [0.33, 0.33, 0.33]
        assert abs(math.fsum(values) - 1.0) > 0.01
        result = build(
            CHOICE, {"probabilities": dict(zip(CHOICE.labels, values))}, max_sum_error=0.01
        )
        assert math.fsum(result["probabilities"].values()) == pytest.approx(1.0)

    def test_limit_applies_to_score(self):
        with pytest.raises(AnswerRejected, match="must sum to 1"):
            build(SCORE, {"probabilities": {"0": 0.5, "1": 0.5, "2": 0.5}}, max_sum_error=0.01)

    def test_limit_does_not_apply_to_noul(self):
        assert build(NOUL, {"p_yes": 0.4}, max_sum_error=0.01)["noul"] == 0.4
