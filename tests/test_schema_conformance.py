"""Every emitted body is validated against the pinned wire schemas.

The schemas come from ``contract/schemas/wire-*.json``, which wrap the pinned OpenAPI
components as Draft 2020-12 documents without adding limits.

Fixture provenance is kept apart here, and the test names say which layer they check:

- ``official-doc-example-*`` are documentation examples, not observations.
- ``recorded-historical-typesafe-*`` are historical ``speed_latest`` traffic, not current Jev.
- everything the daemon produces in this file is a locally synthesized case.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os

import pytest
from jsonschema import Draft202012Validator

from conftest import CONTRACT_DIR, FIXTURE_DIR, MIXED_REQUEST, load_fixture, load_schema

PINNED_OPENAPI_SHA256 = "a191f8a7df6bd6fedced8120dd0fd106f88575d1d1c8360d08900a6c7c0360d5"


def validator(schema_name: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(schema_name))


def assert_valid(schema_name: str, instance: object) -> None:
    errors = sorted(validator(schema_name).iter_errors(instance), key=lambda e: e.json_path)
    assert not errors, "\n".join(f"{e.json_path}: {e.message}" for e in errors)


class TestPinnedBundle:
    def test_the_openapi_snapshot_still_hashes_to_the_pin(self) -> None:
        path = os.path.join(CONTRACT_DIR, "sources", "openapi.json")
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        assert digest == PINNED_OPENAPI_SHA256

    def test_the_snapshot_declares_the_pinned_versions(self) -> None:
        path = os.path.join(CONTRACT_DIR, "sources", "openapi.json")
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        assert document["openapi"] == "3.1.0"
        assert document["info"]["version"] == "0.2.0"
        assert set(document["paths"]) == {"/v1/systemone", "/v1/models"}

    def test_every_wire_schema_is_a_usable_draft_2020_12_document(self) -> None:
        names = sorted(
            os.path.basename(path)
            for path in glob.glob(os.path.join(CONTRACT_DIR, "schemas", "wire-*.json"))
        )
        assert len(names) == 16
        for name in names:
            Draft202012Validator.check_schema(load_schema(name))


class TestOfficialDocumentationExamples:
    """Documentation examples. They are not observed traffic and settle no runtime question."""

    @pytest.mark.parametrize(
        "name",
        sorted(
            os.path.basename(path)
            for path in glob.glob(os.path.join(FIXTURE_DIR, "official-doc-example-*.json"))
        ),
    )
    def test_example_validates_against_its_pinned_schema(self, name: str) -> None:
        document = load_fixture(name)
        if "questions" in document:
            assert_valid("wire-SystemOneRequest.json", document)
        else:
            assert_valid("wire-SystemOneResponse.json", document)

    def test_all_eight_examples_are_present(self) -> None:
        found = glob.glob(os.path.join(FIXTURE_DIR, "official-doc-example-*.json"))
        assert len(found) == 8

    def test_the_daemon_accepts_every_documented_request_example(self) -> None:
        from jevmulator import wire

        for path in sorted(glob.glob(os.path.join(FIXTURE_DIR, "official-doc-example-*.json"))):
            document = load_fixture(os.path.basename(path))
            if "questions" not in document:
                continue
            parsed = wire.parse_request(document)
            assert parsed.question_ids


class TestHistoricalRecordedTraffic:
    """Historical ``speed_latest`` traffic. Not a current Jev 1.13 capture."""

    def test_historical_request_validates(self) -> None:
        assert_valid(
            "wire-SystemOneRequest.json", load_fixture("recorded-historical-typesafe-request.json")
        )

    def test_historical_response_validates_despite_its_extra_fields(self) -> None:
        response = load_fixture("recorded-historical-typesafe-response.json")
        assert_valid("wire-SystemOneResponse.json", response)

    def test_historical_extra_fields_are_not_treated_as_required(self) -> None:
        response = load_fixture("recorded-historical-typesafe-response.json")
        assert "assets_used" in response
        schema = load_schema("wire-SystemOneResponse.json")
        required = schema["components"]["schemas"]["SystemOneResponse"]["required"]
        assert set(required) == {"model", "answers", "usage"}
        assert "assets_used" not in required

    def test_the_daemon_accepts_the_historical_request_body(self) -> None:
        from jevmulator import wire

        parsed = wire.parse_request(load_fixture("recorded-historical-typesafe-request.json"))
        assert parsed.model == "speed_latest"


class TestSynthesizedDaemonResponses:
    """Locally synthesized cases produced by this daemon."""

    def test_mixed_response_validates(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        assert response.status == 200, response.body
        assert_valid("wire-SystemOneResponse.json", response.body)

    def test_each_answer_validates_against_its_own_schema(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        answers = response.body["answers"]
        assert_valid("wire-NoulAnswer.json", answers["billing"])
        assert_valid("wire-ChoiceAnswer.json", answers["tone"])
        assert_valid("wire-ScoreAnswer.json", answers["urgency"])

    def test_models_response_validates(self, daemon) -> None:
        response = daemon.client.get_models()
        assert response.status == 200
        assert_valid("wire-ModelMetadataList.json", response.body)

    def test_usage_validates(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        assert_valid("wire-Usage.json", response.body["usage"])

    def test_validation_error_body_validates(self, daemon) -> None:
        response = daemon.client.post_evaluate({"model": "jev-latest", "questions": {}})
        assert response.status == 422
        assert_valid("wire-HTTPValidationError.json", response.body)

    def test_one_level_score_response_validates(self, daemon) -> None:
        request = {
            "model": "jev-latest",
            "state": "one level only",
            "questions": {"q": {"type": "score", "criteria": ["the only level"]}},
        }
        response = daemon.client.post_evaluate(request)
        assert response.status == 200, response.body
        assert_valid("wire-SystemOneResponse.json", response.body)
        answer = response.body["answers"]["q"]
        assert answer["score"] == 0.0
        assert answer["confidence"] == 1.0

    def test_a_response_to_every_documented_request_example_validates(self, daemon) -> None:
        for path in sorted(glob.glob(os.path.join(FIXTURE_DIR, "official-doc-example-*.json"))):
            document = load_fixture(os.path.basename(path))
            if "questions" not in document:
                continue
            body = dict(document)
            body["model"] = "jev-latest"
            response = daemon.client.post_evaluate(body)
            assert response.status == 200, (path, response.body)
            assert_valid("wire-SystemOneResponse.json", response.body)
            assert set(response.body["answers"]) == set(document["questions"])


class TestSemanticCorrespondence:
    """Shape validity and semantic validity are checked separately, as the contract asks."""

    def test_answer_keys_equal_question_keys(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        assert set(response.body["answers"]) == set(MIXED_REQUEST["questions"])

    def test_each_answer_type_matches_its_question_type(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        for question_id, question in MIXED_REQUEST["questions"].items():
            assert response.body["answers"][question_id]["type"] == question["type"]

    def test_choice_probability_keys_cover_exactly_the_criteria(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        answer = response.body["answers"]["tone"]
        assert set(answer["probabilities"]) == set(MIXED_REQUEST["questions"]["tone"]["criteria"])

    def test_choice_is_an_argmax_of_its_own_distribution(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        answer = response.body["answers"]["tone"]
        best = max(answer["probabilities"].values())
        assert answer["probabilities"][answer["choice"]] == pytest.approx(best)

    def test_score_agrees_with_its_own_distribution(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        answer = response.body["answers"]["urgency"]
        expected = sum(int(k) * v for k, v in answer["probabilities"].items())
        assert answer["score"] == pytest.approx(expected)

    def test_score_legend_keys_match_probability_keys(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        answer = response.body["answers"]["urgency"]
        assert set(answer["legend"]) == set(answer["probabilities"])

    def test_score_legend_repeats_the_original_structured_levels(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        levels = MIXED_REQUEST["questions"]["urgency"]["criteria"]
        assert response.body["answers"]["urgency"]["legend"] == {
            str(index): value for index, value in enumerate(levels)
        }

    def test_probabilities_sum_to_approximately_one(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        for question_id in ("tone", "urgency"):
            total = sum(response.body["answers"][question_id]["probabilities"].values())
            assert abs(total - 1.0) < 0.02

    def test_every_number_stays_in_range(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        answers = response.body["answers"]
        assert 0.0 <= answers["billing"]["noul"] <= 1.0
        assert 0.0 <= answers["tone"]["confidence"] <= 1.0
        assert 0.0 <= answers["urgency"]["confidence"] <= 1.0
        levels = len(MIXED_REQUEST["questions"]["urgency"]["criteria"])
        assert 0.0 <= answers["urgency"]["score"] <= levels - 1
