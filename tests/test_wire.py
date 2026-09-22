"""Request validation against the pinned OpenAPI schema.

Where the documentation prose is stricter than the schema, the schema wins. Those cases
are asserted here so a later change cannot quietly tighten the wire contract.
"""

from __future__ import annotations

import pytest

from jevmulator import wire
from jevmulator.errors import RequestValidationError


def locations(error: RequestValidationError) -> list[list]:
    return [entry["loc"] for entry in error.details]


def types(error: RequestValidationError) -> list[str]:
    return [entry["type"] for entry in error.details]


class TestContentUnion:
    @pytest.mark.parametrize(
        "value", ["text", "", {}, {"a": 1}, [], [1, None, True], {"n": [1, {"d": None}]}]
    )
    def test_accepts_string_object_and_array(self, value: object) -> None:
        assert wire.is_content(value) is True

    @pytest.mark.parametrize("value", [None, 1, 1.5, True, False])
    def test_rejects_scalars_that_are_not_strings(self, value: object) -> None:
        assert wire.is_content(value) is False

    def test_null_is_content_or_null_but_not_content(self) -> None:
        assert wire.is_content_or_null(None) is True
        assert wire.is_content(None) is False


class TestRequiredFields:
    def test_rejects_a_non_object_body(self) -> None:
        with pytest.raises(RequestValidationError) as caught:
            wire.parse_request([1, 2, 3])
        assert locations(caught.value) == [["body"]]

    def test_names_every_missing_field(self) -> None:
        with pytest.raises(RequestValidationError) as caught:
            wire.parse_request({})
        assert locations(caught.value) == [
            ["body", "state"],
            ["body", "model"],
            ["body", "questions"],
        ]
        assert set(types(caught.value)) == {"missing"}

    def test_rejects_a_null_state(self) -> None:
        with pytest.raises(RequestValidationError) as caught:
            wire.parse_request({"state": None, "model": "m", "questions": {"q": {"type": "noul"}}})
        assert ["body", "state"] in locations(caught.value)

    @pytest.mark.parametrize("state", [1, 1.5, True])
    def test_rejects_a_scalar_state(self, state: object) -> None:
        with pytest.raises(RequestValidationError):
            wire.parse_request({"state": state, "model": "m", "questions": {"q": {"type": "noul"}}})

    def test_accepts_an_empty_string_state(self) -> None:
        """The schema sets no length minimum, so an empty string is valid."""
        parsed = wire.parse_request(
            {"state": "", "model": "m", "questions": {"q": {"type": "noul"}}}
        )
        assert parsed.state == ""

    def test_accepts_an_empty_object_state(self) -> None:
        parsed = wire.parse_request(
            {"state": {}, "model": "m", "questions": {"q": {"type": "noul"}}}
        )
        assert parsed.state == {}

    def test_rejects_an_empty_questions_object(self) -> None:
        with pytest.raises(RequestValidationError) as caught:
            wire.parse_request({"state": "s", "model": "m", "questions": {}})
        assert ["body", "questions"] in locations(caught.value)
        assert "too_short" in types(caught.value)

    def test_rejects_a_non_string_model(self) -> None:
        with pytest.raises(RequestValidationError) as caught:
            wire.parse_request({"state": "s", "model": 7, "questions": {"q": {"type": "noul"}}})
        assert ["body", "model"] in locations(caught.value)


class TestDiscriminator:
    def test_rejects_a_question_without_a_type(self) -> None:
        with pytest.raises(RequestValidationError) as caught:
            wire.parse_request({"state": "s", "model": "m", "questions": {"q": {}}})
        assert "union_tag_not_found" in types(caught.value)

    def test_rejects_an_unknown_type(self) -> None:
        with pytest.raises(RequestValidationError) as caught:
            wire.parse_request(
                {"state": "s", "model": "m", "questions": {"q": {"type": "vibe"}}}
            )
        assert "union_tag_invalid" in types(caught.value)

    def test_reports_the_question_id_in_the_location(self) -> None:
        with pytest.raises(RequestValidationError) as caught:
            wire.parse_request(
                {"state": "s", "model": "m", "questions": {"my-id": {"type": "vibe"}}}
            )
        assert locations(caught.value)[0][:3] == ["body", "questions", "my-id"]


class TestNoulQuestion:
    def test_accepts_a_bare_noul(self) -> None:
        """Only ``type`` is required, so a noul question with nothing else is valid."""
        parsed = wire.parse_request(
            {"state": "s", "model": "m", "questions": {"q": {"type": "noul"}}}
        )
        question = parsed.questions["q"]
        assert isinstance(question, wire.NoulQuestion)
        assert question.instructions is None
        assert question.criteria is None

    def test_accepts_explicit_null_instructions(self) -> None:
        """The prose page marks instructions required; the OpenAPI and both SDKs do not."""
        parsed = wire.parse_request(
            {
                "state": "s",
                "model": "m",
                "questions": {"q": {"type": "noul", "instructions": None}},
            }
        )
        assert parsed.questions["q"].instructions is None

    def test_accepts_structured_instructions(self) -> None:
        parsed = wire.parse_request(
            {
                "state": "s",
                "model": "m",
                "questions": {"q": {"type": "noul", "instructions": {"task": "find spam"}}},
            }
        )
        assert parsed.questions["q"].instructions == {"task": "find spam"}

    def test_accepts_partial_criteria(self) -> None:
        parsed = wire.parse_request(
            {
                "state": "s",
                "model": "m",
                "questions": {"q": {"type": "noul", "criteria": {"true": "yes side"}}},
            }
        )
        assert parsed.questions["q"].criteria == {"true": "yes side"}

    def test_accepts_null_criteria_values(self) -> None:
        parsed = wire.parse_request(
            {
                "state": "s",
                "model": "m",
                "questions": {"q": {"type": "noul", "criteria": {"true": None, "false": None}}},
            }
        )
        assert parsed.questions["q"].criteria == {"true": None, "false": None}

    def test_rejects_an_extra_criteria_key(self) -> None:
        with pytest.raises(RequestValidationError) as caught:
            wire.parse_request(
                {
                    "state": "s",
                    "model": "m",
                    "questions": {"q": {"type": "noul", "criteria": {"maybe": "x"}}},
                }
            )
        assert "extra_forbidden" in types(caught.value)

    @pytest.mark.parametrize("bad", [1, 1.5, True])
    def test_rejects_a_scalar_instruction(self, bad: object) -> None:
        with pytest.raises(RequestValidationError):
            wire.parse_request(
                {
                    "state": "s",
                    "model": "m",
                    "questions": {"q": {"type": "noul", "instructions": bad}},
                }
            )


class TestChoiceQuestion:
    def test_requires_criteria(self) -> None:
        with pytest.raises(RequestValidationError) as caught:
            wire.parse_request(
                {"state": "s", "model": "m", "questions": {"q": {"type": "choice"}}}
            )
        assert "missing" in types(caught.value)

    def test_accepts_a_null_description(self) -> None:
        """A choice without a description is interpreted by its name alone."""
        parsed = wire.parse_request(
            {
                "state": "s",
                "model": "m",
                "questions": {"q": {"type": "choice", "criteria": {"a": None, "b": "b side"}}},
            }
        )
        assert parsed.questions["q"].labels == ["a", "b"]

    def test_accepts_structured_descriptions(self) -> None:
        parsed = wire.parse_request(
            {
                "state": "s",
                "model": "m",
                "questions": {
                    "q": {"type": "choice", "criteria": {"a": {"when": "x"}, "b": ["y", "z"]}}
                },
            }
        )
        assert parsed.questions["q"].criteria["a"] == {"when": "x"}

    def test_accepts_an_empty_criteria_object(self) -> None:
        """The schema sets no minimum property count. Runtime acceptance upstream is unknown."""
        parsed = wire.parse_request(
            {"state": "s", "model": "m", "questions": {"q": {"type": "choice", "criteria": {}}}}
        )
        assert parsed.questions["q"].labels == []

    def test_preserves_label_order(self) -> None:
        parsed = wire.parse_request(
            {
                "state": "s",
                "model": "m",
                "questions": {
                    "q": {"type": "choice", "criteria": {"zeta": None, "alpha": None, "mu": None}}
                },
            }
        )
        assert parsed.questions["q"].labels == ["zeta", "alpha", "mu"]

    def test_rejects_a_numeric_description(self) -> None:
        with pytest.raises(RequestValidationError) as caught:
            wire.parse_request(
                {
                    "state": "s",
                    "model": "m",
                    "questions": {"q": {"type": "choice", "criteria": {"a": 3}}},
                }
            )
        assert locations(caught.value)[0] == ["body", "questions", "q", "choice", "criteria", "a"]


class TestScoreQuestion:
    def test_requires_criteria(self) -> None:
        with pytest.raises(RequestValidationError):
            wire.parse_request({"state": "s", "model": "m", "questions": {"q": {"type": "score"}}})

    def test_rejects_an_empty_criteria_list(self) -> None:
        with pytest.raises(RequestValidationError) as caught:
            wire.parse_request(
                {"state": "s", "model": "m", "questions": {"q": {"type": "score", "criteria": []}}}
            )
        assert "too_short" in types(caught.value)

    def test_accepts_one_level(self) -> None:
        """minItems is 1. The docs recommend two, and runtime behaviour upstream is unknown."""
        parsed = wire.parse_request(
            {
                "state": "s",
                "model": "m",
                "questions": {"q": {"type": "score", "criteria": ["only"]}},
            }
        )
        assert parsed.questions["q"].level_keys == ["0"]

    def test_accepts_structured_levels_and_preserves_them(self) -> None:
        levels = ["Can wait", {"label": "soon"}, ["today", "now"]]
        parsed = wire.parse_request(
            {"state": "s", "model": "m", "questions": {"q": {"type": "score", "criteria": levels}}}
        )
        question = parsed.questions["q"]
        assert question.legend() == {"0": levels[0], "1": levels[1], "2": levels[2]}
        assert question.level_keys == ["0", "1", "2"]

    def test_rejects_a_null_level(self) -> None:
        with pytest.raises(RequestValidationError) as caught:
            wire.parse_request(
                {
                    "state": "s",
                    "model": "m",
                    "questions": {"q": {"type": "score", "criteria": ["a", None]}},
                }
            )
        assert locations(caught.value)[0] == ["body", "questions", "q", "score", "criteria", 1]

    def test_rejects_a_numeric_level(self) -> None:
        with pytest.raises(RequestValidationError):
            wire.parse_request(
                {"state": "s", "model": "m", "questions": {"q": {"type": "score", "criteria": [1]}}}
            )

    def test_accepts_ten_levels(self) -> None:
        parsed = wire.parse_request(
            {
                "state": "s",
                "model": "m",
                "questions": {"q": {"type": "score", "criteria": [f"level {i}" for i in range(10)]}},
            }
        )
        assert len(parsed.questions["q"].level_keys) == 10


class TestUnspecifiedProperties:
    def test_extra_top_level_properties_are_permitted(self) -> None:
        """No pinned request schema sets additionalProperties false."""
        parsed = wire.parse_request(
            {
                "state": "s",
                "model": "m",
                "questions": {"q": {"type": "noul"}},
                "temperature": 0.3,
            }
        )
        assert parsed.model == "m"

    def test_extra_question_properties_are_permitted(self) -> None:
        parsed = wire.parse_request(
            {
                "state": "s",
                "model": "m",
                "questions": {"q": {"type": "noul", "weight": 3}},
            }
        )
        assert isinstance(parsed.questions["q"], wire.NoulQuestion)


class TestErrorAccumulation:
    def test_reports_every_bad_question_not_only_the_first(self) -> None:
        with pytest.raises(RequestValidationError) as caught:
            wire.parse_request(
                {
                    "state": "s",
                    "model": "m",
                    "questions": {
                        "a": {"type": "score", "criteria": []},
                        "b": {"type": "choice"},
                        "c": {"type": "noul", "instructions": 5},
                    },
                }
            )
        reported = {entry["loc"][2] for entry in caught.value.details}
        assert reported == {"a", "b", "c"}


class TestResponseBuilders:
    def test_noul_answer_has_no_confidence(self) -> None:
        answer = wire.noul_answer(0.98)
        assert answer == {"type": "noul", "noul": 0.98}
        assert "confidence" not in answer

    def test_choice_answer_shape(self) -> None:
        answer = wire.choice_answer("angry", 0.9, {"angry": 0.8, "calm": 0.2})
        assert set(answer) == {"type", "choice", "confidence", "probabilities"}
        assert answer["type"] == "choice"

    def test_score_answer_keeps_structured_legend(self) -> None:
        legend = {"0": "Can wait", "1": {"label": "today"}}
        answer = wire.score_answer(0.7, 0.5, legend, {"0": 0.3, "1": 0.7})
        assert answer["legend"] == legend
        assert set(answer["probabilities"]) == set(legend)

    def test_response_usage_is_integers(self) -> None:
        response = wire.system_one_response("m", {"q": wire.noul_answer(0.5)}, 12.0, 3.0)
        assert isinstance(response["usage"]["input_tokens"], int)
        assert isinstance(response["usage"]["output_tokens"], int)
