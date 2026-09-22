"""Retained raw consumers, re-expressed as checks against a real daemon.

These are the downstream expectations recorded in `contract/consumer-compatibility.md`.
They are client behaviour, not provider authority. Where a client is stricter than the
pinned wire contract, the test says so and asserts the narrowing rather than changing the
daemon.

Each validator below reproduces the retained client's own rules. The clients themselves
address a hardcoded provider hostname, so they are exercised here through their documented
endpoint or transport seam, which for this test means pointing the same rules at this
daemon's output.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from conftest import MIXED_REQUEST, start_daemon

STRING_LEGEND_REQUEST: dict[str, Any] = {
    "model": "jev-latest",
    "state": "The payout for order 4417 failed three times since Monday.",
    "questions": {
        "billing": {"type": "noul", "instructions": "Is this about billing?"},
        "tone": {
            "type": "choice",
            "instructions": "What is the tone?",
            "criteria": {"angry": "upset", "calm": "neutral", "excited": "eager"},
        },
        "urgency": {
            "type": "score",
            "instructions": "How urgent is this?",
            "criteria": ["Can wait", "Needs attention this week", "Needs attention today"],
        },
    },
}


# -- jev-ultrafast ------------------------------------------------------


def validate_choice_like_jev_ultrafast(answer: dict[str, Any], candidates: list[str]) -> None:
    """The retained `jev-ultrafast` choice validator.

    Exact probability-key coverage, finite values in [0, 1], a sum error strictly below
    0.02, and a selected choice that agrees with the argmax.
    """
    probabilities = answer["probabilities"]
    assert set(probabilities) == set(candidates), "probability keys must cover the candidates"
    for value in probabilities.values():
        assert isinstance(value, (int, float)) and not isinstance(value, bool)
        assert math.isfinite(value)
        assert 0.0 <= value <= 1.0
    assert abs(sum(probabilities.values()) - 1.0) < 0.02, "sum error must be below 0.02"
    best = max(probabilities.values())
    assert probabilities[answer["choice"]] == pytest.approx(best), "choice must be an argmax"


class TestJevUltrafast:
    def test_the_choice_validator_passes(self, daemon) -> None:
        response = daemon.client.post_evaluate(STRING_LEGEND_REQUEST)
        assert response.status == 200
        validate_choice_like_jev_ultrafast(
            response.body["answers"]["tone"],
            list(STRING_LEGEND_REQUEST["questions"]["tone"]["criteria"]),
        )

    def test_the_validator_passes_for_structured_criteria_too(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        validate_choice_like_jev_ultrafast(
            response.body["answers"]["tone"],
            list(MIXED_REQUEST["questions"]["tone"]["criteria"]),
        )

    def test_structured_instructions_and_descriptions_are_accepted(self, daemon) -> None:
        """jev-ultrafast sends objects in instructions and choice descriptions."""
        request = {
            "model": "jev-latest",
            "state": {"ticket": {"body": "Refund please", "tags": ["billing"]}},
            "questions": {
                "route": {
                    "type": "choice",
                    "instructions": {"task": "route this ticket", "team_hint": None},
                    "criteria": {
                        "billing": {"when": "money is involved"},
                        "technical": ["errors", "outages"],
                    },
                }
            },
        }
        response = daemon.client.post_evaluate(request)
        assert response.status == 200, response.body
        validate_choice_like_jev_ultrafast(
            response.body["answers"]["route"], ["billing", "technical"]
        )

    def test_the_model_field_is_readable(self, daemon) -> None:
        response = daemon.client.post_evaluate(STRING_LEGEND_REQUEST)
        assert isinstance(response.body["model"], str)
        assert response.body["model"]

    def test_the_validator_rejects_a_deliberately_broken_distribution(self) -> None:
        """The validator is real: it fails when the distribution is wrong."""
        with pytest.raises(AssertionError):
            validate_choice_like_jev_ultrafast(
                {"choice": "a", "probabilities": {"a": 0.3, "b": 0.3}}, ["a", "b"]
            )
        with pytest.raises(AssertionError):
            validate_choice_like_jev_ultrafast(
                {"choice": "b", "probabilities": {"a": 0.9, "b": 0.1}}, ["a", "b"]
            )


# -- jev-review ---------------------------------------------------------


def validate_response_like_jev_review(payload: dict[str, Any], *, strict_legend: bool) -> None:
    """The retained `jev-review` Zod schema.

    Requires ``model``, ``answers`` and nonnegative integer usage counts, discriminates
    answers by ``type``, and accepts only string score legend values.
    """
    assert isinstance(payload["model"], str)
    assert isinstance(payload["answers"], dict)
    usage = payload["usage"]
    for key in ("input_tokens", "output_tokens"):
        assert isinstance(usage[key], int) and not isinstance(usage[key], bool)
        assert usage[key] >= 0

    for answer in payload["answers"].values():
        kind = answer["type"]
        if kind == "noul":
            assert isinstance(answer["noul"], (int, float))
        elif kind == "choice":
            assert isinstance(answer["choice"], str)
            assert isinstance(answer["confidence"], (int, float))
            assert isinstance(answer["probabilities"], dict)
        elif kind == "score":
            assert isinstance(answer["score"], (int, float))
            assert isinstance(answer["confidence"], (int, float))
            assert isinstance(answer["probabilities"], dict)
            if strict_legend:
                for value in answer["legend"].values():
                    assert isinstance(value, str), "jev-review accepts string legends only"
        else:
            raise AssertionError(f"unknown answer type {kind}")


class TestJevReview:
    def test_a_string_legend_response_satisfies_the_schema(self, daemon) -> None:
        response = daemon.client.post_evaluate(STRING_LEGEND_REQUEST)
        assert response.status == 200
        validate_response_like_jev_review(response.body, strict_legend=True)

    def test_usage_counts_are_nonnegative_integers(self, daemon) -> None:
        response = daemon.client.post_evaluate(STRING_LEGEND_REQUEST)
        usage = response.body["usage"]
        assert isinstance(usage["input_tokens"], int)
        assert isinstance(usage["output_tokens"], int)
        assert usage["input_tokens"] >= 0 and usage["output_tokens"] >= 0

    def test_a_structured_legend_is_valid_on_the_wire_but_narrows_this_client(
        self, daemon
    ) -> None:
        """Documented client narrowing, not a daemon defect.

        The pinned OpenAPI permits object and array level descriptions. jev-review's own
        schema accepts string values only. A caller that sends structured levels must
        widen that client, or send string levels.
        """
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        validate_response_like_jev_review(response.body, strict_legend=False)
        with pytest.raises(AssertionError, match="string legends only"):
            validate_response_like_jev_review(response.body, strict_legend=True)

    def test_the_error_body_exposes_error_type(self) -> None:
        """jev-review reads detail.error_type looking for max_tokens_exceeded."""
        with start_daemon(
            JEVMULATOR_PROVIDER="fake", JEVMULATOR_MAX_STATE_CHARS="50"
        ) as running:
            response = running.client.post_evaluate(
                dict(STRING_LEGEND_REQUEST, state="z" * 500)
            )
            assert response.status == 422
            assert response.body["detail"]["error_type"] == "max_tokens_exceeded"

    @pytest.mark.parametrize("status", [429, 529, 502])
    def test_retryable_statuses_are_produced_for_the_client_to_retry(
        self, status: int, upstream_factory
    ) -> None:
        """jev-review retries 429, 529 and other 5xx. The daemon must be able to emit them."""
        upstream_status = {429: 429, 529: 503, 502: 500}[status]
        upstream = upstream_factory([{"status": upstream_status, "json": {"error": "x"}}])
        with start_daemon(
            JEVMULATOR_PROVIDER="openai",
            JEVMULATOR_UPSTREAM_BASE_URL=upstream.base_url,
            JEVMULATOR_UPSTREAM_API_KEY="k",
            JEVMULATOR_UPSTREAM_API_KEY_ENV="A_VARIABLE_THAT_IS_NOT_SET",
            JEVMULATOR_UPSTREAM_RETRIES="0",
        ) as running:
            response = running.client.post_evaluate(STRING_LEGEND_REQUEST)
            assert response.status == status


# -- fast-jev-compaction ------------------------------------------------


def parse_response_like_fast_jev_compaction(payload: dict[str, Any]) -> dict[str, Any]:
    """The retained `fast-jev-compaction` parser: it requires only an ``answers`` object."""
    answers = payload["answers"]
    assert isinstance(answers, dict)
    return answers


def noul_answer_like_fast_jev_compaction(answers: dict[str, Any], key: str) -> float:
    """Its ``noulAnswer``: a finite number, with no range check."""
    value = answers[key]["noul"]
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    assert math.isfinite(value)
    return float(value)


class TestFastJevCompaction:
    def test_the_permissive_parser_accepts_the_response(self, daemon) -> None:
        response = daemon.client.post_evaluate(STRING_LEGEND_REQUEST)
        answers = parse_response_like_fast_jev_compaction(response.body)
        assert set(answers) == set(STRING_LEGEND_REQUEST["questions"])

    def test_noul_answers_are_finite(self, daemon) -> None:
        response = daemon.client.post_evaluate(STRING_LEGEND_REQUEST)
        answers = parse_response_like_fast_jev_compaction(response.body)
        assert math.isfinite(noul_answer_like_fast_jev_compaction(answers, "billing"))

    def test_the_base_url_seam_is_a_complete_endpoint(self, daemon) -> None:
        """Its baseUrl is a complete endpoint URL, so it points straight at /v1/systemone."""
        endpoint = daemon.client.base_url + "/v1/systemone"
        assert endpoint.endswith("/v1/systemone")
        response = daemon.client.request("POST", "/v1/systemone", STRING_LEGEND_REQUEST)
        assert response.status == 200

    def test_a_permissive_parser_cannot_certify_parity(self, daemon) -> None:
        """Recorded limitation: this parser accepts a body the wire schema rejects."""
        answers = parse_response_like_fast_jev_compaction(
            {"answers": {"q": {"type": "noul", "noul": 7.5}}}
        )
        assert noul_answer_like_fast_jev_compaction(answers, "q") == 7.5


# -- Foreman ------------------------------------------------------------


def normalize_like_foreman(value: Any) -> float:
    """Foreman's ``normalize_assessment``: reject booleans and nonfinite, clamp into [0, 1]."""
    assert not isinstance(value, bool), "Foreman rejects booleans"
    assert isinstance(value, (int, float)), "Foreman rejects non-numbers"
    assert math.isfinite(value), "Foreman rejects nonfinite values"
    return min(1.0, max(0.0, float(value)))


class TestForeman:
    ASSESSMENTS = [f"assessment_{index}" for index in range(10)]

    def test_all_ten_assessments_come_back(self, daemon) -> None:
        request = {
            "model": "jev-latest",
            "state": "A long customer message about a failed payout.",
            "questions": {
                name: {"type": "noul", "instructions": f"Does condition {name} hold?"}
                for name in self.ASSESSMENTS
            },
        }
        response = daemon.client.post_evaluate(request)
        assert response.status == 200, response.body
        assert set(response.body["answers"]) == set(self.ASSESSMENTS)

    def test_every_value_survives_foreman_normalisation(self, daemon) -> None:
        request = {
            "model": "jev-latest",
            "state": "A long customer message about a failed payout.",
            "questions": {
                name: {"type": "noul", "instructions": f"Does condition {name} hold?"}
                for name in self.ASSESSMENTS
            },
        }
        response = daemon.client.post_evaluate(request)
        for name in self.ASSESSMENTS:
            value = normalize_like_foreman(response.body["answers"][name]["noul"])
            assert 0.0 <= value <= 1.0

    def test_clamping_is_consumer_behaviour_not_provider_permission(self, daemon) -> None:
        """Foreman clamps out-of-range values. The daemon never emits one to be clamped."""
        request = {
            "model": "jev-latest",
            "state": "A message.",
            "questions": {"only": {"type": "noul", "instructions": "Is this a message?"}},
        }
        response = daemon.client.post_evaluate(request)
        raw = response.body["answers"]["only"]["noul"]
        assert 0.0 <= raw <= 1.0
        assert normalize_like_foreman(raw) == raw

    def test_the_normaliser_is_real(self) -> None:
        with pytest.raises(AssertionError, match="booleans"):
            normalize_like_foreman(True)
        with pytest.raises(AssertionError, match="nonfinite"):
            normalize_like_foreman(float("nan"))
        assert normalize_like_foreman(1.4) == 1.0


# -- NanoJev ------------------------------------------------------------


class TestNanoJevIsNotEmulated:
    """NanoJev is a reference implementation. Its envelope must not reach this contract."""

    def test_the_nanojev_route_is_absent(self, daemon) -> None:
        response = daemon.client.request("POST", "/api/evaluate", {"states": []})
        assert response.status == 404

    def test_the_daemon_rejects_a_nanojev_style_body(self, daemon) -> None:
        response = daemon.client.post_evaluate({"states": [], "questions": []})
        assert response.status == 422

    def test_no_nanojev_limit_is_imposed(self, daemon) -> None:
        """NanoJev caps questions at 96. The pinned contract sets no maximum, so neither do we."""
        request = {
            "model": "jev-latest",
            "state": "A message.",
            "questions": {
                f"q{index:03d}": {"type": "noul", "instructions": f"Is fact {index} present?"}
                for index in range(120)
            },
        }
        response = daemon.client.post_evaluate(request, timeout=120)
        assert response.status == 200
        assert len(response.body["answers"]) == 120
