"""The sys1 form and brief: aliasing, the submission schema, every rejection, and isolation.

These run without a daemon. The HTTP-level behaviour is in test_sys1_http.py.
"""

from __future__ import annotations

import json
import math

import pytest

from jevmulator import answers, wire
from jevmulator.sys1 import brief, form
from jevmulator.sys1.profiles import load_profiles

REQUEST = {
    "model": "jev-latest",
    "state": {"ticket": "I was charged twice for order 4417."},
    "questions": {
        "billing": {"type": "noul", "instructions": "Is this about billing?",
                    "criteria": {"true": "money", "false": "anything else"}},
        "tone": {"type": "choice", "instructions": "What is the tone?",
                 "criteria": {"angry": "upset", "calm": None, "excited": {"why": "eager"}}},
        "urgency": {"type": "score", "criteria": ["Can wait", "This week", "Today"]},
    },
}

VALID = {
    "answers": {
        "q1": {"p_yes": 0.9},
        "q2": {"probabilities": {"angry": 0.6, "calm": 0.3, "excited": 0.1}},
        "q3": {"probabilities": {"0": 0.1, "1": 0.3, "2": 0.6}},
    },
    "rationale": "The ticket names a duplicate charge.",
    "evidence": ["state.ticket"],
}


def aliased(payload=REQUEST):
    return form.alias_request(wire.parse_request(payload))


def validate(submission, **kwargs):
    kwargs.setdefault("normalize", True)
    kwargs.setdefault("tolerance", 1e-6)
    kwargs.setdefault("max_sum_error", 0.01)
    return form.validate_submission(submission, aliased(), **kwargs)


def renamed(payload, mapping):
    copy = json.loads(json.dumps(payload))
    copy["questions"] = {mapping[key]: value for key, value in copy["questions"].items()}
    return copy


class TestAliasing:
    def test_labels_follow_request_order(self):
        result = aliased()
        assert result.aliases == ("q1", "q2", "q3")
        assert result.question_ids == ("billing", "tone", "urgency")
        assert result.id_for("q2") == "tone"

    def test_documents_hold_labels_and_never_ids(self):
        # IDs that cannot occur in the content by chance. "billing" would: the question
        # text itself says "billing".
        ids = {"billing": "id-7f3a91", "tone": "id-b22c04", "urgency": "id-e9d515"}
        result = aliased(renamed(REQUEST, ids))
        text = json.dumps(form.questions_document(result)) + json.dumps(form.submission_schema(result))
        for question_id in ids.values():
            assert question_id not in text

    def test_renamed_ids_leave_every_agent_input_identical(self):
        # Question IDs are routing labels the contract keeps out of model input. Renaming
        # them must change nothing the agent can read.
        first = aliased(REQUEST)
        second = aliased(renamed(REQUEST, {"billing": "zq-771", "tone": "zq-772", "urgency": "zq-773"}))
        assert form.questions_document(first) == form.questions_document(second)
        assert form.submission_schema(first) == form.submission_schema(second)
        assert brief.render_questions(first) == brief.render_questions(second)
        profile = load_profiles()["read-only"]
        texts = [
            brief.render_brief(
                profile, item, REQUEST["state"], state_path="S", workdir="W",
                tool_names=["read"], max_submissions=3, max_sum_error=0.01,
            )
            for item in (first, second)
        ]
        assert texts[0] == texts[1]

    def test_zero_option_choice_is_unanswerable(self):
        payload = {"model": "x", "state": "s", "questions": {
            "a": {"type": "noul"}, "b": {"type": "choice", "criteria": {}}}}
        assert form.unanswerable_aliases(aliased(payload)) == ["q2"]


class TestSchema:
    def test_answers_require_every_label_and_nothing_else(self):
        schema = form.submission_schema(aliased())
        answers_schema = schema["properties"]["answers"]
        assert answers_schema["required"] == ["q1", "q2", "q3"]
        assert answers_schema["additionalProperties"] is False
        assert schema["required"] == ["answers", "rationale"]
        choice = answers_schema["properties"]["q2"]["properties"]["probabilities"]
        assert choice["required"] == ["angry", "calm", "excited"]

    def test_questions_document_names_probability_keys(self):
        document = form.questions_document(aliased())
        assert "probability_keys" not in document[0]
        assert document[1]["probability_keys"] == ["angry", "calm", "excited"]
        assert document[2]["probability_keys"] == ["0", "1", "2"]


class TestValidation:
    def test_valid_submission_builds_wire_answers(self):
        result = validate(VALID)
        assert result.accepted
        assert result.answers["q1"] == {"type": "noul", "noul": 0.9}
        assert result.answers["q2"]["choice"] == "angry"
        assert result.answers["q3"]["score"] == pytest.approx(1.5)
        assert result.rationale == VALID["rationale"]

    def test_derived_fields_equal_the_bare_computation(self):
        result = validate(VALID)
        question = aliased().by_alias()["q3"]
        bare = answers.build_answer(question, VALID["answers"]["q3"], normalize=True, tolerance=1e-6)
        assert result.answers["q3"] == bare

    def test_every_problem_is_reported_at_once(self):
        bad = {
            "answers": {
                "q1": {"p_yes": 1.4},
                "q2": {"probabilities": {"angry": 0.3, "calm": 0.3, "excited": 0.3}},
                "q9": {"p_yes": 0.5},
            },
            "rationale": "",
            "confidence": 0.9,
        }
        result = validate(bad)
        assert not result.accepted
        paths = {problem["path"] for problem in result.problems}
        assert paths == {"confidence", "rationale", "answers.q9", "answers.q1", "answers.q2", "answers.q3"}
        messages = {problem["path"]: problem["problem"] for problem in result.problems}
        assert "outside the range 0 to 1" in messages["answers.q1"]
        assert "sum to 0.9" in messages["answers.q2"]
        assert "missing" in messages["answers.q3"]

    @pytest.mark.parametrize(
        "change, path, fragment",
        [
            (lambda s: s["answers"]["q2"]["probabilities"].update({"bored": 0.0}), "answers.q2", "never requested: 'bored'"),
            (lambda s: s["answers"]["q2"]["probabilities"].pop("calm"), "answers.q2", "left out requested candidates: 'calm'"),
            (lambda s: s["answers"]["q3"]["probabilities"].update({"0": True}), "answers.q3", "not a finite number"),
            (lambda s: s["answers"]["q3"].update({"probabilities": {"0": 0, "1": 0, "2": 0}}), "answers.q3", "no outcome was given any weight"),
            (lambda s: s["answers"].update({"q1": 0.9}), "answers.q1", "must be an object"),
            (lambda s: s.update({"evidence": "one string"}), "evidence", "list of strings"),
            (lambda s: s.update({"answers": []}), "answers", "must be an object"),
        ],
    )
    def test_each_rejection_names_its_path(self, change, path, fragment):
        submission = json.loads(json.dumps(VALID))
        change(submission)
        result = validate(submission)
        assert not result.accepted
        problems = {problem["path"]: problem["problem"] for problem in result.problems}
        assert fragment in problems[path]

    def test_non_object_submission(self):
        result = validate(["not", "an", "object"])
        assert not result.accepted
        assert result.problems[0]["path"] == ""

    def test_thirds_written_as_0_33_pass_and_are_rescaled(self):
        submission = json.loads(json.dumps(VALID))
        submission["answers"]["q2"]["probabilities"] = {"angry": 0.33, "calm": 0.33, "excited": 0.33}
        result = validate(submission)
        assert result.accepted
        assert math.fsum(result.answers["q2"]["probabilities"].values()) == pytest.approx(1.0)

    def test_small_error_is_not_rescaled_when_normalisation_is_off(self):
        submission = json.loads(json.dumps(VALID))
        submission["answers"]["q2"]["probabilities"] = {"angry": 0.33, "calm": 0.33, "excited": 0.33}
        result = validate(submission, normalize=False)
        assert result.accepted
        assert result.answers["q2"]["probabilities"]["angry"] == 0.33


class TestBrief:
    def test_brief_carries_state_questions_and_rules(self):
        profile = load_profiles()["read-only"]
        text = brief.render_brief(
            profile, aliased(), REQUEST["state"], state_path="C:/runs/x/state.json",
            workdir="C:/runs/x/work", tool_names=["find", "grep", "ls", "read"],
            max_submissions=3, max_sum_error=0.01,
        )
        assert "C:/runs/x/state.json" in text
        assert "I was charged twice" in text
        assert '"angry": upset' in text
        assert '"calm": no description' in text
        assert 'Answer shape: {"p_yes"' in text
        assert "You have 3 submissions" in text
        assert "within 0.01" in text
        assert "$" not in text.replace("$schema", "")

    def test_a_long_state_is_referenced_not_repeated(self):
        block = brief.render_state_block("x" * (brief.STATE_INLINE_LIMIT + 1))
        assert "too long to repeat" in block
        assert "xxxx" not in block

    def test_read_roots_are_named(self):
        profile = load_profiles()["read-only"]
        confined = profile.__class__(**{**profile.__dict__, "read_roots": ("C:\\repo",)})
        text = brief.render_brief(
            confined, aliased(), "s", state_path="S", workdir="W", tool_names=["read"],
            max_submissions=3, max_sum_error=0.01,
        )
        assert "only inside these directories: `C:\\repo`" in text
