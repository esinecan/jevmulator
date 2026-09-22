"""Question independence and question-ID invisibility.

The pinned contract says two things that no schema can express. Questions are evaluated
independently, and question IDs are routing labels that are not supplied to the model.

These cases assert both against the **actual upstream payloads**, captured through
``GET /_jevmulator/debug/upstream-calls``. Comparing only the answers would prove nothing:
two runs can agree by chance. Comparing the payload the model received proves the
property directly.

Question IDs here are random tokens that cannot appear anywhere else in the request, so a
substring search over the payload is meaningful. A readable ID such as ``billing`` would
match the word ``billing`` inside the caller's own instructions and give a false positive.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import pytest

from conftest import chat_completion, start_daemon

STATE = "The payout for order 4417 failed three times since Monday."


def token() -> str:
    """A question ID that cannot occur anywhere else in the request."""
    return "qid" + secrets.token_hex(8)


def noul_question() -> dict[str, Any]:
    return {"type": "noul", "instructions": "Does this describe a payment failure?"}


def choice_question() -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": "What is the tone?",
        "criteria": {"angry": "upset", "calm": "neutral", "excited": "eager"},
    }


def score_question() -> dict[str, Any]:
    return {
        "type": "score",
        "instructions": "How urgent is this?",
        "criteria": ["Can wait", "This week", "Today"],
    }


def payload_for(calls: list[dict[str, Any]], marker: str) -> dict[str, Any]:
    """The single recorded upstream call whose user message contains ``marker``."""
    matches = [call for call in calls if marker in call["messages"][1]["content"]]
    assert len(matches) == 1, f"expected one call carrying {marker!r}, found {len(matches)}"
    return matches[0]


class TestQuestionIdsNeverReachTheModel:
    def test_no_question_id_appears_in_any_upstream_payload(self, daemon) -> None:
        ids = [token() for _ in range(3)]
        request = {
            "model": "jev-latest",
            "state": STATE,
            "questions": {
                ids[0]: noul_question(),
                ids[1]: choice_question(),
                ids[2]: score_question(),
            },
        }
        assert daemon.client.post_evaluate(request).status == 200
        recorded = json.dumps(daemon.client.recorded_calls())
        for question_id in ids:
            assert question_id not in recorded

    def test_the_upstream_answer_key_is_the_fixed_literal(self, daemon) -> None:
        question_id = token()
        request = {
            "model": "jev-latest",
            "state": STATE,
            "questions": {question_id: choice_question()},
        }
        daemon.client.post_evaluate(request)
        call = daemon.client.recorded_calls()[0]
        assert list(call["schema"]["properties"]) == ["answer"]
        assert question_id not in json.dumps(call["schema"])

    def test_choice_labels_do_reach_the_model(self, daemon) -> None:
        """Option labels are semantic input under the pinned contract. IDs are not."""
        request = {
            "model": "jev-latest",
            "state": STATE,
            "questions": {token(): choice_question()},
        }
        daemon.client.post_evaluate(request)
        content = daemon.client.recorded_calls()[0]["messages"][1]["content"]
        for label in ("angry", "calm", "excited"):
            assert label in content

    def test_one_upstream_call_is_made_per_question(self, daemon) -> None:
        request = {
            "model": "jev-latest",
            "state": STATE,
            "questions": {token(): noul_question(), token(): choice_question()},
        }
        daemon.client.post_evaluate(request)
        assert len(daemon.client.recorded_calls()) == 2

    def test_no_payload_carries_a_sibling_question(self, daemon) -> None:
        request = {
            "model": "jev-latest",
            "state": STATE,
            "questions": {token(): noul_question(), token(): choice_question()},
        }
        daemon.client.post_evaluate(request)
        calls = daemon.client.recorded_calls()
        noul_call = payload_for(calls, "payment failure")
        choice_call = payload_for(calls, "What is the tone?")
        assert "What is the tone?" not in json.dumps(noul_call)
        assert "payment failure" not in json.dumps(choice_call)


class TestMetamorphicRenaming:
    def test_renaming_a_question_id_leaves_its_upstream_payload_identical(self, daemon) -> None:
        first_id, second_id = token(), token()
        base = {"model": "jev-latest", "state": STATE}

        daemon.client.clear_recorded()
        daemon.client.post_evaluate({**base, "questions": {first_id: choice_question()}})
        first_calls = daemon.client.recorded_calls()

        daemon.client.clear_recorded()
        daemon.client.post_evaluate({**base, "questions": {second_id: choice_question()}})
        second_calls = daemon.client.recorded_calls()

        assert first_calls == second_calls

    def test_renaming_does_not_change_the_answer(self, daemon) -> None:
        first_id, second_id = token(), token()
        base = {"model": "jev-latest", "state": STATE}
        first = daemon.client.post_evaluate({**base, "questions": {first_id: score_question()}})
        second = daemon.client.post_evaluate({**base, "questions": {second_id: score_question()}})
        assert first.body["answers"][first_id] == second.body["answers"][second_id]

    def test_the_answer_comes_back_under_the_submitted_id(self, daemon) -> None:
        question_id = token()
        response = daemon.client.post_evaluate(
            {"model": "jev-latest", "state": STATE, "questions": {question_id: noul_question()}}
        )
        assert list(response.body["answers"]) == [question_id]


class TestMetamorphicAddition:
    def test_adding_an_unrelated_question_leaves_the_first_payload_identical(
        self, daemon
    ) -> None:
        kept_id = token()
        base = {"model": "jev-latest", "state": STATE}

        daemon.client.clear_recorded()
        daemon.client.post_evaluate({**base, "questions": {kept_id: choice_question()}})
        alone = payload_for(daemon.client.recorded_calls(), "What is the tone?")

        daemon.client.clear_recorded()
        daemon.client.post_evaluate(
            {
                **base,
                "questions": {
                    kept_id: choice_question(),
                    token(): noul_question(),
                    token(): score_question(),
                },
            }
        )
        together = payload_for(daemon.client.recorded_calls(), "What is the tone?")

        assert alone == together

    def test_adding_an_unrelated_question_leaves_the_first_answer_identical(
        self, daemon
    ) -> None:
        kept_id = token()
        base = {"model": "jev-latest", "state": STATE}
        alone = daemon.client.post_evaluate({**base, "questions": {kept_id: choice_question()}})
        together = daemon.client.post_evaluate(
            {**base, "questions": {kept_id: choice_question(), token(): score_question()}}
        )
        assert alone.body["answers"][kept_id] == together.body["answers"][kept_id]

    def test_removing_a_question_leaves_the_others_identical(self, daemon) -> None:
        kept_id, dropped_id = token(), token()
        base = {"model": "jev-latest", "state": STATE}
        with_both = daemon.client.post_evaluate(
            {**base, "questions": {kept_id: score_question(), dropped_id: noul_question()}}
        )
        without = daemon.client.post_evaluate({**base, "questions": {kept_id: score_question()}})
        assert with_both.body["answers"][kept_id] == without.body["answers"][kept_id]


class TestMetamorphicOrdering:
    def test_reordering_questions_leaves_every_payload_identical(self, daemon) -> None:
        first_id, second_id, third_id = token(), token(), token()
        base = {"model": "jev-latest", "state": STATE}

        daemon.client.clear_recorded()
        daemon.client.post_evaluate(
            {
                **base,
                "questions": {
                    first_id: noul_question(),
                    second_id: choice_question(),
                    third_id: score_question(),
                },
            }
        )
        forward = sorted(
            json.dumps(call, sort_keys=True) for call in daemon.client.recorded_calls()
        )

        daemon.client.clear_recorded()
        daemon.client.post_evaluate(
            {
                **base,
                "questions": {
                    third_id: score_question(),
                    second_id: choice_question(),
                    first_id: noul_question(),
                },
            }
        )
        reversed_order = sorted(
            json.dumps(call, sort_keys=True) for call in daemon.client.recorded_calls()
        )

        assert forward == reversed_order

    def test_reordering_questions_leaves_every_answer_identical(self, daemon) -> None:
        first_id, second_id = token(), token()
        base = {"model": "jev-latest", "state": STATE}
        forward = daemon.client.post_evaluate(
            {**base, "questions": {first_id: noul_question(), second_id: score_question()}}
        )
        backward = daemon.client.post_evaluate(
            {**base, "questions": {second_id: score_question(), first_id: noul_question()}}
        )
        assert forward.body["answers"] == backward.body["answers"]

    def test_reordering_choice_labels_changes_the_payload(self, daemon) -> None:
        """Label order is caller-supplied semantic input, so it is allowed to matter."""
        base = {"model": "jev-latest", "state": STATE}
        forward_question = {
            "type": "choice",
            "instructions": "Pick one.",
            "criteria": {"a": None, "b": None},
        }
        reversed_question = {
            "type": "choice",
            "instructions": "Pick one.",
            "criteria": {"b": None, "a": None},
        }

        daemon.client.clear_recorded()
        daemon.client.post_evaluate({**base, "questions": {token(): forward_question}})
        forward = daemon.client.recorded_calls()[0]["messages"][1]["content"]

        daemon.client.clear_recorded()
        daemon.client.post_evaluate({**base, "questions": {token(): reversed_question}})
        backward = daemon.client.recorded_calls()[0]["messages"][1]["content"]

        assert forward != backward


class TestIsolationOverRealUpstreamHttp:
    """The same property, proved against a real upstream HTTP server rather than the fake."""

    def test_the_upstream_server_never_sees_a_question_id(self, upstream_factory) -> None:
        upstream = upstream_factory(
            [{"status": 200, "json": chat_completion('{"answer": {"p_yes": 0.5}}')}]
        )
        ids = [token(), token()]
        with start_daemon(
            JEVMULATOR_PROVIDER="openai",
            JEVMULATOR_UPSTREAM_BASE_URL=upstream.base_url,
            JEVMULATOR_UPSTREAM_API_KEY="test-upstream-key",
            JEVMULATOR_UPSTREAM_API_KEY_ENV="A_VARIABLE_THAT_IS_NOT_SET",
        ) as running:
            response = running.client.post_evaluate(
                {
                    "model": "jev-latest",
                    "state": STATE,
                    "questions": {ids[0]: noul_question(), ids[1]: noul_question()},
                }
            )
            assert response.status == 200
        received = json.dumps(upstream.script.received)
        for question_id in ids:
            assert question_id not in received
        assert upstream.call_count == 2

    def test_each_upstream_request_carries_exactly_one_question(
        self, upstream_factory
    ) -> None:
        upstream = upstream_factory(
            [{"status": 200, "json": chat_completion('{"answer": {"p_yes": 0.5}}')}]
        )
        with start_daemon(
            JEVMULATOR_PROVIDER="openai",
            JEVMULATOR_UPSTREAM_BASE_URL=upstream.base_url,
            JEVMULATOR_UPSTREAM_API_KEY="test-upstream-key",
            JEVMULATOR_UPSTREAM_API_KEY_ENV="A_VARIABLE_THAT_IS_NOT_SET",
        ) as running:
            running.client.post_evaluate(
                {
                    "model": "jev-latest",
                    "state": STATE,
                    "questions": {
                        token(): noul_question(),
                        token(): {
                            "type": "noul",
                            "instructions": "Is the customer a repeat caller?",
                        },
                    },
                }
            )
        contents = [
            call["body"]["messages"][1]["content"] for call in upstream.script.received
        ]
        assert len(contents) == 2
        first = [c for c in contents if "payment failure" in c]
        second = [c for c in contents if "repeat caller" in c]
        assert len(first) == 1 and len(second) == 1
        assert "repeat caller" not in first[0]
        assert "payment failure" not in second[0]


class TestDeterminismIsNotClaimedForLiveModels:
    def test_the_fake_provider_is_deterministic_by_construction(self, daemon) -> None:
        """The fake is deterministic so the metamorphic cases are meaningful.

        This says nothing about a live model. Temperature zero does not make a language
        model reproducible across runs, and nothing in this repository claims it does.
        """
        request = {
            "model": "jev-latest",
            "state": STATE,
            "questions": {token(): score_question()},
        }
        first = daemon.client.post_evaluate(request)
        second = daemon.client.post_evaluate(request)
        first_answer = next(iter(first.body["answers"].values()))
        second_answer = next(iter(second.body["answers"].values()))
        assert first_answer == second_answer

    def test_a_different_state_gives_a_different_payload(self, daemon) -> None:
        question_id = token()
        daemon.client.clear_recorded()
        daemon.client.post_evaluate(
            {"model": "jev-latest", "state": STATE, "questions": {question_id: noul_question()}}
        )
        first = daemon.client.recorded_calls()[0]
        daemon.client.clear_recorded()
        daemon.client.post_evaluate(
            {
                "model": "jev-latest",
                "state": "Something else entirely.",
                "questions": {question_id: noul_question()},
            }
        )
        second = daemon.client.recorded_calls()[0]
        assert first != second


@pytest.mark.parametrize("question_builder", [noul_question, choice_question, score_question])
def test_every_primitive_keeps_the_id_out_of_the_payload(daemon, question_builder) -> None:
    question_id = token()
    daemon.client.clear_recorded()
    response = daemon.client.post_evaluate(
        {"model": "jev-latest", "state": STATE, "questions": {question_id: question_builder()}}
    )
    assert response.status == 200
    assert question_id not in json.dumps(daemon.client.recorded_calls())
