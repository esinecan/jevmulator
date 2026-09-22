"""The pinned official Python SDK, driven over real HTTP against this daemon.

Pin: ``typesafe-sdk==0.7.1``, contract commit ``0ffd094c72ed9445223060b24ffd7a56aa781fb4``.

Install it with::

    python -m pip install -e ".[dev]" -c constraints-dev.txt

The whole module skips when the SDK is absent, so the offline suites still run.
"""

from __future__ import annotations

import math

import pytest

from conftest import start_daemon

typesafe_sdk = pytest.importorskip("typesafe_sdk", reason="typesafe-sdk==0.7.1 is not installed")

pytestmark = pytest.mark.sdk

STATE = {
    "subject": "Duplicate charge",
    "message": "I was charged twice for order 4417. Please refund one of them.",
}


@pytest.fixture
def sdk_client():
    """A real SDK client pointed at a real daemon on loopback."""
    with start_daemon(JEVMULATOR_PROVIDER="fake", JEVMULATOR_DEBUG_RECORD="1") as running:
        client = typesafe_sdk.TypeSafeClient(
            api_key=running.client.api_key, base_url=running.client.base_url
        )
        try:
            yield client, running
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                close()


class TestSdkVersion:
    def test_the_installed_sdk_is_the_pinned_version(self) -> None:
        assert typesafe_sdk.__version__ == "0.7.1"


class TestSdkEvaluation:
    def test_a_noul_question_round_trips(self, sdk_client) -> None:
        client, _running = sdk_client
        response = client.system_one(
            state=STATE,
            questions={"billing": typesafe_sdk.Noul(instructions="Is this about billing?")},
        )
        answer = response.answers["billing"]
        assert answer.type == "noul"
        assert 0.0 <= answer.noul <= 1.0

    def test_a_choice_question_round_trips(self, sdk_client) -> None:
        client, _running = sdk_client
        response = client.system_one(
            state=STATE,
            questions={
                "tone": typesafe_sdk.Choice(
                    instructions="What is the tone?",
                    criteria={"angry": "upset", "calm": "neutral", "excited": "eager"},
                )
            },
        )
        answer = response.answers["tone"]
        assert answer.type == "choice"
        assert answer.choice in ("angry", "calm", "excited")
        assert set(answer.probabilities) == {"angry", "calm", "excited"}
        assert 0.0 <= answer.confidence <= 1.0

    def test_a_score_question_round_trips(self, sdk_client) -> None:
        client, _running = sdk_client
        response = client.system_one(
            state=STATE,
            questions={
                "urgency": typesafe_sdk.Score(
                    instructions="How urgent is this?",
                    criteria=["Can wait", "This week", "Today"],
                )
            },
        )
        answer = response.answers["urgency"]
        assert answer.type == "score"
        assert 0.0 <= answer.score <= 2.0
        assert 0.0 <= answer.confidence <= 1.0

    def test_a_mixed_batch_round_trips(self, sdk_client) -> None:
        client, _running = sdk_client
        response = client.system_one(
            state=STATE,
            questions={
                "billing": typesafe_sdk.Noul(instructions="Is this about billing?"),
                "tone": typesafe_sdk.Choice(
                    instructions="What is the tone?",
                    criteria={"angry": "upset", "calm": None},
                ),
                "urgency": typesafe_sdk.Score(
                    instructions="How urgent is this?", criteria=["later", "now"]
                ),
            },
        )
        assert set(response.answers) == {"billing", "tone", "urgency"}
        assert response.answers["billing"].type == "noul"
        assert response.answers["tone"].type == "choice"
        assert response.answers["urgency"].type == "score"

    def test_the_sdk_default_model_alias_is_accepted(self, sdk_client) -> None:
        """The SDK inserts ``jev-latest`` before transmission when the caller sets no model."""
        client, running = sdk_client
        client.system_one(
            state="a message", questions={"q": typesafe_sdk.Noul(instructions="Is it text?")}
        )
        assert running.state.metrics()["requests_failed"] == 0

    def test_the_reported_model_is_the_emulator_identity(self, sdk_client) -> None:
        client, _running = sdk_client
        response = client.system_one(
            state="a message", questions={"q": typesafe_sdk.Noul(instructions="Is it text?")}
        )
        assert response.model == "jevmulator-0.1.0-glm-5.3-flash"

    def test_usage_parses_as_integers(self, sdk_client) -> None:
        client, _running = sdk_client
        response = client.system_one(
            state="a message", questions={"q": typesafe_sdk.Noul(instructions="Is it text?")}
        )
        assert isinstance(response.usage.input_tokens, int)
        assert isinstance(response.usage.output_tokens, int)


class TestSdkStructuredContent:
    def test_structured_instructions_survive(self, sdk_client) -> None:
        client, _running = sdk_client
        response = client.system_one(
            state={"ticket": {"body": "refund please", "tags": ["billing", "urgent"]}},
            questions={
                "route": typesafe_sdk.Choice(
                    instructions={"task": "route this ticket"},
                    criteria={"billing": {"when": "money"}, "technical": ["errors"]},
                )
            },
        )
        assert response.answers["route"].choice in ("billing", "technical")

    def test_omitted_instructions_are_accepted(self, sdk_client) -> None:
        """The OpenAPI and both SDKs make instructions optional. The prose page does not."""
        client, _running = sdk_client
        response = client.system_one(
            state="a message",
            questions={"q": typesafe_sdk.Choice(criteria={"yes": None, "no": None})},
        )
        assert response.answers["q"].choice in ("yes", "no")

    def test_a_score_legend_comes_back_keyed_by_level(self, sdk_client) -> None:
        client, _running = sdk_client
        levels = ["Can wait", "This week", "Today"]
        response = client.system_one(
            state="a message",
            questions={"urgency": typesafe_sdk.Score(instructions="How urgent?", criteria=levels)},
        )
        legend = response.answers["urgency"].legend
        # The Python SDK converts score map keys to integers; the HTTP JSON keys are strings.
        assert {str(key): value for key, value in legend.items()} == {
            "0": levels[0],
            "1": levels[1],
            "2": levels[2],
        }

    def test_score_probability_keys_match_the_legend(self, sdk_client) -> None:
        client, _running = sdk_client
        response = client.system_one(
            state="a message",
            questions={
                "urgency": typesafe_sdk.Score(instructions="How urgent?", criteria=["a", "b", "c"])
            },
        )
        answer = response.answers["urgency"]
        assert set(map(str, answer.probabilities)) == set(map(str, answer.legend))

    def test_the_expected_score_agrees_with_the_returned_distribution(self, sdk_client) -> None:
        client, _running = sdk_client
        response = client.system_one(
            state="a message",
            questions={
                "urgency": typesafe_sdk.Score(instructions="How urgent?", criteria=["a", "b", "c"])
            },
        )
        answer = response.answers["urgency"]
        expected = math.fsum(
            int(level) * probability for level, probability in answer.probabilities.items()
        )
        assert answer.score == pytest.approx(expected)


class TestSdkModelDiscovery:
    def test_list_models_returns_the_catalogue(self, sdk_client) -> None:
        client, _running = sdk_client
        models = client.models.list()
        names = [model.name for model in models.models]
        assert "jev-latest" in names
        assert "jevmulator-0.1.0-glm-5.3-flash" in names

    def test_every_description_names_the_upstream_model(self, sdk_client) -> None:
        client, _running = sdk_client
        for model in client.models.list().models:
            assert "glm-5.3-flash" in model.description

    def test_release_dates_parse_as_dates(self, sdk_client) -> None:
        client, _running = sdk_client
        for model in client.models.list().models:
            assert len(model.release_date) == 10
            assert model.release_date.count("-") == 2


class TestSdkIsolation:
    def test_the_sdk_path_still_keeps_question_ids_from_the_model(self, sdk_client) -> None:
        import json
        import secrets

        client, running = sdk_client
        question_id = "qid" + secrets.token_hex(8)
        running.client.clear_recorded()
        client.system_one(
            state="a message",
            questions={question_id: typesafe_sdk.Noul(instructions="Is it text?")},
        )
        assert question_id not in json.dumps(running.client.recorded_calls())


class TestSdkErrorHandling:
    def test_a_bad_key_raises(self) -> None:
        with start_daemon(JEVMULATOR_PROVIDER="fake") as running:
            client = typesafe_sdk.TypeSafeClient(
                api_key="not-the-key", base_url=running.client.base_url
            )
            with pytest.raises(Exception) as caught:
                client.system_one(
                    state="a message", questions={"q": typesafe_sdk.Noul(instructions="Is it text?")}
                )
            assert "401" in str(caught.value) or "auth" in str(caught.value).lower()

    def test_an_unknown_model_raises(self) -> None:
        with start_daemon(JEVMULATOR_PROVIDER="fake") as running:
            client = typesafe_sdk.TypeSafeClient(
                api_key=running.client.api_key, base_url=running.client.base_url
            )
            with pytest.raises(Exception):
                client.system_one(
                    state="a message",
                    model="gpt-4o-mini",
                    questions={"q": typesafe_sdk.Noul(instructions="Is it text?")},
                )

    def test_an_upstream_failure_raises_rather_than_returning_a_judgment(self) -> None:
        with start_daemon(
            JEVMULATOR_PROVIDER="fake",
            JEVMULATOR_FAKE_MODE="invalid_json",
            JEVMULATOR_REPAIR_RETRIES="0",
        ) as running:
            client = typesafe_sdk.TypeSafeClient(
                api_key=running.client.api_key, base_url=running.client.base_url
            )
            with pytest.raises(Exception):
                client.system_one(
                    state="a message", questions={"q": typesafe_sdk.Noul(instructions="Is it text?")}
                )

    def test_a_rate_limited_upstream_surfaces_as_an_error(self, upstream_factory) -> None:
        upstream = upstream_factory([{"status": 429, "json": {"error": "slow down"}}])
        with start_daemon(
            JEVMULATOR_PROVIDER="openai",
            JEVMULATOR_UPSTREAM_BASE_URL=upstream.base_url,
            JEVMULATOR_UPSTREAM_API_KEY="k",
            JEVMULATOR_UPSTREAM_API_KEY_ENV="A_VARIABLE_THAT_IS_NOT_SET",
            JEVMULATOR_UPSTREAM_RETRIES="0",
            JEVMULATOR_RETRY_BACKOFF_SECONDS="0.01",
        ) as running:
            client = typesafe_sdk.TypeSafeClient(
                api_key=running.client.api_key,
                base_url=running.client.base_url,
                retry=typesafe_sdk.RetryPolicy(max_retries=0),
            )
            with pytest.raises(Exception):
                client.system_one(
                    state="a message", questions={"q": typesafe_sdk.Noul(instructions="Is it text?")}
                )
