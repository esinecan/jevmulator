"""Upstream failure handling, driven by a controlled fake upstream HTTP server.

These cases prove the rule the contract review asked for: a model failure or a transport
failure never becomes a successful invented judgment. Every case here speaks real HTTP to
a loopback server under the test's control. No live provider is reached.
"""

from __future__ import annotations

import json

import pytest

from conftest import MIXED_REQUEST, chat_completion, start_daemon

NOUL_REQUEST = {
    "model": "jev-latest",
    "state": "The payout failed three times today.",
    "questions": {"urgent": {"type": "noul", "instructions": "Is this urgent?"}},
}

CHOICE_REQUEST = {
    "model": "jev-latest",
    "state": "Thanks, this is exactly what I needed.",
    "questions": {
        "tone": {
            "type": "choice",
            "instructions": "What is the tone?",
            "criteria": {"angry": None, "calm": None, "excited": None},
        }
    },
}


def daemon_against(upstream, **env: str):
    return start_daemon(
        JEVMULATOR_PROVIDER="openai",
        JEVMULATOR_UPSTREAM_BASE_URL=upstream.base_url,
        JEVMULATOR_UPSTREAM_API_KEY="test-upstream-key",
        JEVMULATOR_UPSTREAM_API_KEY_ENV="A_VARIABLE_THAT_IS_NOT_SET",
        JEVMULATOR_RETRY_BACKOFF_SECONDS="0.01",
        JEVMULATOR_RETRY_BACKOFF_MAX_SECONDS="0.05",
        **env,
    )


def answer(content: str) -> dict:
    return {"status": 200, "json": chat_completion(content)}


class TestHappyPath:
    def test_a_valid_upstream_answer_becomes_a_valid_wire_answer(self, upstream_factory) -> None:
        upstream = upstream_factory([answer('{"answer": {"p_yes": 0.87}}')])
        with daemon_against(upstream) as running:
            response = running.client.post_evaluate(NOUL_REQUEST)
            assert response.status == 200, response.body
            assert response.body["answers"]["urgent"]["noul"] == pytest.approx(0.87)
            assert response.body["usage"] == {"input_tokens": 31, "output_tokens": 7}

    def test_the_upstream_request_carries_the_configured_model(self, upstream_factory) -> None:
        upstream = upstream_factory([answer('{"answer": {"p_yes": 0.5}}')])
        with daemon_against(upstream, JEVMULATOR_UPSTREAM_MODEL="glm-5.3-flash") as running:
            running.client.post_evaluate(NOUL_REQUEST)
        assert upstream.script.received[0]["body"]["model"] == "glm-5.3-flash"

    def test_the_upstream_request_disables_thinking_by_default(self, upstream_factory) -> None:
        upstream = upstream_factory([answer('{"answer": {"p_yes": 0.5}}')])
        with daemon_against(upstream) as running:
            running.client.post_evaluate(NOUL_REQUEST)
        assert upstream.script.received[0]["body"]["thinking"] == {"type": "disabled"}

    def test_thinking_can_be_omitted_for_endpoints_that_reject_it(self, upstream_factory) -> None:
        upstream = upstream_factory([answer('{"answer": {"p_yes": 0.5}}')])
        with daemon_against(upstream, JEVMULATOR_UPSTREAM_THINKING="omit") as running:
            running.client.post_evaluate(NOUL_REQUEST)
        assert "thinking" not in upstream.script.received[0]["body"]

    def test_the_upstream_request_uses_a_strict_json_schema_by_default(
        self, upstream_factory
    ) -> None:
        upstream = upstream_factory([answer('{"answer": {"p_yes": 0.5}}')])
        with daemon_against(upstream) as running:
            running.client.post_evaluate(NOUL_REQUEST)
        response_format = upstream.script.received[0]["body"]["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["strict"] is True

    def test_json_object_mode_sends_the_simpler_response_format(self, upstream_factory) -> None:
        upstream = upstream_factory([answer('{"answer": {"p_yes": 0.5}}')])
        with daemon_against(upstream, JEVMULATOR_RESPONSE_FORMAT="json_object") as running:
            running.client.post_evaluate(NOUL_REQUEST)
        assert upstream.script.received[0]["body"]["response_format"] == {"type": "json_object"}

    def test_prompted_mode_sends_no_response_format_and_states_the_schema(
        self, upstream_factory
    ) -> None:
        upstream = upstream_factory([answer('{"answer": {"p_yes": 0.5}}')])
        with daemon_against(upstream, JEVMULATOR_RESPONSE_FORMAT="none") as running:
            running.client.post_evaluate(NOUL_REQUEST)
        body = upstream.script.received[0]["body"]
        assert "response_format" not in body
        assert "JSON Schema" in body["messages"][0]["content"]

    def test_a_code_fenced_answer_is_accepted(self, upstream_factory) -> None:
        upstream = upstream_factory(
            [answer('```json\n{"answer": {"p_yes": 0.25}}\n```')]
        )
        with daemon_against(upstream, JEVMULATOR_RESPONSE_FORMAT="none") as running:
            response = running.client.post_evaluate(NOUL_REQUEST)
            assert response.status == 200
            assert response.body["answers"]["urgent"]["noul"] == pytest.approx(0.25)

    def test_the_upstream_key_is_sent_only_in_the_authorization_header(
        self, upstream_factory
    ) -> None:
        upstream = upstream_factory([answer('{"answer": {"p_yes": 0.5}}')])
        with daemon_against(upstream) as running:
            running.client.post_evaluate(NOUL_REQUEST)
        received = upstream.script.received[0]
        assert received["headers"]["Authorization"] == "Bearer test-upstream-key"
        assert "test-upstream-key" not in json.dumps(received["body"])


class TestMalformedUpstreamOutput:
    @pytest.mark.parametrize(
        ("content", "reason"),
        [
            ("this is not json", "not valid JSON"),
            ('{"result": {"p_yes": 0.4}}', "no object under the key"),
            ('{"answer": {"probability": 0.4}}', "no 'p_yes' key"),
            ('{"answer": {"p_yes": "high"}}', "not a finite number"),
            ('{"answer": {"p_yes": 1.4}}', "outside the range"),
            ('{"answer": {"p_yes": true}}', "not a finite number"),
            ('{"answer": []}', "no object under the key"),
            ("", "empty"),
        ],
    )
    def test_a_bad_noul_answer_is_502_and_never_a_judgment(
        self, upstream_factory, content: str, reason: str
    ) -> None:
        upstream = upstream_factory([answer(content)])
        with daemon_against(upstream, JEVMULATOR_REPAIR_RETRIES="0") as running:
            response = running.client.post_evaluate(NOUL_REQUEST)
            assert response.status == 502
            assert response.body["detail"]["error_type"] == "upstream_invalid_output"
            assert "answers" not in response.body

    @pytest.mark.parametrize(
        "content",
        [
            '{"answer": {"probabilities": {"angry": 0.5, "calm": 0.5}}}',
            '{"answer": {"probabilities": {"angry": 0.3, "calm": 0.3, "excited": 0.3, "bored": 0.1}}}',
            '{"answer": {"probabilities": {"angry": 0.0, "calm": 0.0, "excited": 0.0}}}',
            '{"answer": {"probabilities": {"angry": -0.2, "calm": 0.6, "excited": 0.6}}}',
            '{"answer": {"probabilities": {"angry": "a lot", "calm": 0.5, "excited": 0.5}}}',
        ],
    )
    def test_a_broken_choice_distribution_is_502(self, upstream_factory, content: str) -> None:
        upstream = upstream_factory([answer(content)])
        with daemon_against(upstream, JEVMULATOR_REPAIR_RETRIES="0") as running:
            response = running.client.post_evaluate(CHOICE_REQUEST)
            assert response.status == 502
            assert response.body["detail"]["error_type"] == "upstream_invalid_output"

    def test_an_unknown_candidate_is_named_in_the_message(self, upstream_factory) -> None:
        upstream = upstream_factory(
            [answer('{"answer": {"probabilities": {"angry": 0.3, "calm": 0.3, "excited": 0.3, "smug": 0.1}}}')]
        )
        with daemon_against(upstream, JEVMULATOR_REPAIR_RETRIES="0") as running:
            response = running.client.post_evaluate(CHOICE_REQUEST)
            assert "smug" in response.body["detail"]["message"]

    def test_a_missing_candidate_is_named_in_the_message(self, upstream_factory) -> None:
        upstream = upstream_factory(
            [answer('{"answer": {"probabilities": {"angry": 0.5, "calm": 0.5}}}')]
        )
        with daemon_against(upstream, JEVMULATOR_REPAIR_RETRIES="0") as running:
            response = running.client.post_evaluate(CHOICE_REQUEST)
            assert "excited" in response.body["detail"]["message"]

    def test_an_unnormalised_distribution_is_rescaled_not_rejected(
        self, upstream_factory
    ) -> None:
        upstream = upstream_factory(
            [answer('{"answer": {"probabilities": {"angry": 0.2, "calm": 0.2, "excited": 0.2}}}')]
        )
        with daemon_against(upstream) as running:
            response = running.client.post_evaluate(CHOICE_REQUEST)
            assert response.status == 200
            probabilities = response.body["answers"]["tone"]["probabilities"]
            assert sum(probabilities.values()) == pytest.approx(1.0)

    def test_normalisation_can_be_turned_off(self, upstream_factory) -> None:
        upstream = upstream_factory(
            [answer('{"answer": {"probabilities": {"angry": 0.2, "calm": 0.2, "excited": 0.2}}}')]
        )
        with daemon_against(upstream, JEVMULATOR_NORMALIZE_PROBABILITIES="0") as running:
            response = running.client.post_evaluate(CHOICE_REQUEST)
            assert response.status == 200
            probabilities = response.body["answers"]["tone"]["probabilities"]
            assert sum(probabilities.values()) == pytest.approx(0.6)


class TestRepairRetries:
    def test_one_corrective_re_ask_recovers_a_bad_answer(self, upstream_factory) -> None:
        upstream = upstream_factory(
            [answer("not json at all"), answer('{"answer": {"p_yes": 0.42}}')]
        )
        with daemon_against(upstream, JEVMULATOR_REPAIR_RETRIES="1") as running:
            response = running.client.post_evaluate(NOUL_REQUEST)
            assert response.status == 200
            assert response.body["answers"]["urgent"]["noul"] == pytest.approx(0.42)
        assert upstream.call_count == 2

    def test_the_re_ask_states_the_reason(self, upstream_factory) -> None:
        upstream = upstream_factory(
            [answer("not json at all"), answer('{"answer": {"p_yes": 0.42}}')]
        )
        with daemon_against(upstream, JEVMULATOR_REPAIR_RETRIES="1") as running:
            running.client.post_evaluate(NOUL_REQUEST)
        second = upstream.script.received[1]["body"]["messages"]
        assert second[-1]["role"] == "user"
        assert "could not be used" in second[-1]["content"]
        assert second[-2]["role"] == "assistant"

    def test_repair_retries_are_bounded(self, upstream_factory) -> None:
        upstream = upstream_factory([answer("never valid")])
        with daemon_against(upstream, JEVMULATOR_REPAIR_RETRIES="2") as running:
            response = running.client.post_evaluate(NOUL_REQUEST)
            assert response.status == 502
        assert upstream.call_count == 3

    def test_zero_repair_retries_means_one_attempt(self, upstream_factory) -> None:
        upstream = upstream_factory([answer("never valid")])
        with daemon_against(upstream, JEVMULATOR_REPAIR_RETRIES="0") as running:
            assert running.client.post_evaluate(NOUL_REQUEST).status == 502
        assert upstream.call_count == 1


class TestRefusal:
    def test_an_explicit_refusal_is_502(self, upstream_factory) -> None:
        upstream = upstream_factory(
            [{"status": 200, "json": chat_completion("", refusal="I cannot help with that.")}]
        )
        with daemon_against(upstream) as running:
            response = running.client.post_evaluate(NOUL_REQUEST)
            assert response.status == 502
            assert response.body["detail"]["error_type"] == "upstream_refusal"

    def test_a_content_filter_finish_reason_is_502(self, upstream_factory) -> None:
        upstream = upstream_factory(
            [{"status": 200, "json": chat_completion("", finish_reason="content_filter")}]
        )
        with daemon_against(upstream) as running:
            response = running.client.post_evaluate(NOUL_REQUEST)
            assert response.status == 502
            assert response.body["detail"]["error_type"] == "upstream_refusal"

    def test_a_refusal_is_not_retried(self, upstream_factory) -> None:
        upstream = upstream_factory(
            [{"status": 200, "json": chat_completion("", refusal="no")}]
        )
        with daemon_against(upstream, JEVMULATOR_UPSTREAM_RETRIES="3") as running:
            running.client.post_evaluate(NOUL_REQUEST)
        assert upstream.call_count == 1


class TestTransportFailures:
    def test_upstream_429_is_retried_then_surfaced_as_429(self, upstream_factory) -> None:
        upstream = upstream_factory([{"status": 429, "json": {"error": "slow down"}}])
        with daemon_against(upstream, JEVMULATOR_UPSTREAM_RETRIES="2") as running:
            response = running.client.post_evaluate(NOUL_REQUEST)
            assert response.status == 429
            assert response.body["detail"]["error_type"] == "rate_limit_error"
        assert upstream.call_count == 3

    def test_a_retry_after_header_is_forwarded(self, upstream_factory) -> None:
        upstream = upstream_factory(
            [{"status": 429, "json": {"error": "slow"}, "headers": {"retry-after": "1"}}]
        )
        with daemon_against(upstream, JEVMULATOR_UPSTREAM_RETRIES="0") as running:
            response = running.client.post_evaluate(NOUL_REQUEST)
            assert response.status == 429
            assert response.headers.get("retry-after") == "1"

    def test_upstream_503_becomes_529_overloaded(self, upstream_factory) -> None:
        upstream = upstream_factory([{"status": 503, "json": {"error": "busy"}}])
        with daemon_against(upstream, JEVMULATOR_UPSTREAM_RETRIES="1") as running:
            response = running.client.post_evaluate(NOUL_REQUEST)
            assert response.status == 529
            assert response.body["detail"]["error_type"] == "overloaded"

    def test_upstream_500_becomes_502(self, upstream_factory) -> None:
        upstream = upstream_factory([{"status": 500, "json": {"error": "boom"}}])
        with daemon_against(upstream, JEVMULATOR_UPSTREAM_RETRIES="1") as running:
            response = running.client.post_evaluate(NOUL_REQUEST)
            assert response.status == 502
            assert response.body["detail"]["error_type"] == "upstream_error"

    def test_upstream_400_is_not_retried(self, upstream_factory) -> None:
        upstream = upstream_factory([{"status": 400, "json": {"error": "bad model"}}])
        with daemon_against(upstream, JEVMULATOR_UPSTREAM_RETRIES="3") as running:
            response = running.client.post_evaluate(NOUL_REQUEST)
            assert response.status == 502
        assert upstream.call_count == 1

    def test_a_transient_failure_recovers_within_the_retry_budget(
        self, upstream_factory
    ) -> None:
        upstream = upstream_factory(
            [
                {"status": 503, "json": {"error": "busy"}},
                answer('{"answer": {"p_yes": 0.61}}'),
            ]
        )
        with daemon_against(upstream, JEVMULATOR_UPSTREAM_RETRIES="2") as running:
            response = running.client.post_evaluate(NOUL_REQUEST)
            assert response.status == 200
            assert response.body["answers"]["urgent"]["noul"] == pytest.approx(0.61)
        assert upstream.call_count == 2

    def test_retries_are_bounded(self, upstream_factory) -> None:
        upstream = upstream_factory([{"status": 500, "json": {"error": "boom"}}])
        with daemon_against(upstream, JEVMULATOR_UPSTREAM_RETRIES="2") as running:
            running.client.post_evaluate(NOUL_REQUEST)
        assert upstream.call_count == 3

    def test_an_unparseable_upstream_body_is_retried_then_502(self, upstream_factory) -> None:
        upstream = upstream_factory([{"status": 200, "raw": "<html>gateway</html>"}])
        with daemon_against(upstream, JEVMULATOR_UPSTREAM_RETRIES="1") as running:
            response = running.client.post_evaluate(NOUL_REQUEST)
            assert response.status == 502
        assert upstream.call_count == 2

    def test_an_upstream_body_without_choices_is_502(self, upstream_factory) -> None:
        upstream = upstream_factory([{"status": 200, "json": {"model": "x", "choices": []}}])
        with daemon_against(upstream, JEVMULATOR_UPSTREAM_RETRIES="0") as running:
            assert running.client.post_evaluate(NOUL_REQUEST).status == 502


class TestTimeouts:
    def test_a_slow_upstream_becomes_504(self, upstream_factory) -> None:
        upstream = upstream_factory([answer('{"answer": {"p_yes": 0.5}}')], delay_seconds=2.0)
        with daemon_against(
            upstream,
            JEVMULATOR_UPSTREAM_TIMEOUT_SECONDS="0.4",
            JEVMULATOR_REQUEST_TIMEOUT_SECONDS="1.2",
            JEVMULATOR_UPSTREAM_RETRIES="0",
        ) as running:
            response = running.client.post_evaluate(NOUL_REQUEST, timeout=30)
            assert response.status == 504
            assert response.body["detail"]["error_type"] == "upstream_timeout"

    def test_the_whole_request_deadline_is_honoured(self, upstream_factory) -> None:
        upstream = upstream_factory([answer('{"answer": {"p_yes": 0.5}}')], delay_seconds=1.5)
        with daemon_against(
            upstream,
            JEVMULATOR_UPSTREAM_TIMEOUT_SECONDS="30",
            JEVMULATOR_REQUEST_TIMEOUT_SECONDS="0.7",
            JEVMULATOR_UPSTREAM_RETRIES="0",
        ) as running:
            response = running.client.post_evaluate(MIXED_REQUEST, timeout=30)
            assert response.status == 504

    def test_the_daemon_still_serves_requests_after_a_timeout(self, upstream_factory) -> None:
        upstream = upstream_factory(
            [
                answer('{"answer": {"p_yes": 0.5}}'),
            ],
            delay_seconds=0.0,
        )
        with daemon_against(
            upstream, JEVMULATOR_UPSTREAM_TIMEOUT_SECONDS="5", JEVMULATOR_UPSTREAM_RETRIES="0"
        ) as running:
            assert running.client.post_evaluate(NOUL_REQUEST).status == 200
            assert running.client.status().body["metrics"]["inflight"] == 0


class TestMissingUpstreamCredential:
    def test_no_credential_fails_loudly_instead_of_answering(self) -> None:
        with start_daemon(
            JEVMULATOR_PROVIDER="openai",
            JEVMULATOR_UPSTREAM_BASE_URL="http://127.0.0.1:9/v1",
            JEVMULATOR_UPSTREAM_API_KEY_ENV="A_VARIABLE_THAT_IS_NOT_SET",
        ) as running:
            response = running.client.post_evaluate(NOUL_REQUEST)
            assert response.status == 502
            assert response.body["detail"]["error_type"] == "upstream_not_configured"
            assert "A_VARIABLE_THAT_IS_NOT_SET" in response.body["detail"]["message"]
