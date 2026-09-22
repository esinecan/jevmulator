"""Real loopback HTTP against a running daemon.

Every case here goes over a socket. Nothing calls the evaluator directly, so the routing,
authentication, body reading and header layers are all exercised.
"""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from conftest import MIXED_REQUEST, TEST_API_KEY, start_daemon


class TestModelsRoute:
    def test_returns_the_four_names(self, daemon) -> None:
        response = daemon.client.get_models()
        assert response.status == 200
        names = [model["name"] for model in response.body["models"]]
        assert names == [
            "jev-latest",
            "jev-preview",
            "jevmulator-latest",
            "jevmulator-0.1.0-glm-5.3-flash",
        ]

    def test_every_description_names_the_upstream_model(self, daemon) -> None:
        response = daemon.client.get_models()
        for model in response.body["models"]:
            assert "glm-5.3-flash" in model["description"]
            assert "not by TypeSafe weights" in model["description"]

    def test_release_date_is_the_contract_pin_date(self, daemon) -> None:
        response = daemon.client.get_models()
        assert {model["release_date"] for model in response.body["models"]} == {"2026-09-22"}

    def test_requires_authentication(self, daemon) -> None:
        response = daemon.client.get_models(api_key=None)
        assert response.status == 401

    def test_rejects_post(self, daemon) -> None:
        response = daemon.client.request("POST", "/v1/models", {"a": 1})
        assert response.status == 405


class TestAuthentication:
    def test_missing_header_is_401(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST, api_key=None)
        assert response.status == 401
        assert response.body["detail"]["error_type"] == "authentication_error"

    def test_wrong_key_is_401(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST, api_key="not-the-key")
        assert response.status == 401

    def test_401_carries_a_www_authenticate_header(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST, api_key=None)
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    def test_a_non_bearer_scheme_is_401(self, daemon) -> None:
        response = daemon.client.post_evaluate(
            MIXED_REQUEST, api_key=None, headers={"Authorization": "Basic abcdef"}
        )
        assert response.status == 401

    def test_a_prefix_of_the_key_is_rejected(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST, api_key=TEST_API_KEY[:-1])
        assert response.status == 401

    def test_authentication_is_checked_before_the_body_is_parsed(self, daemon) -> None:
        response = daemon.client.request(
            "POST", "/v1/systemone", raw_body=b"{ not json", api_key=None
        )
        assert response.status == 401

    def test_the_operational_routes_need_no_key(self, daemon) -> None:
        assert daemon.client.health().status == 200
        assert daemon.client.status().status == 200


class TestMalformedRequests:
    def test_invalid_json_is_422(self, daemon) -> None:
        response = daemon.client.request("POST", "/v1/systemone", raw_body=b"{ not json")
        assert response.status == 422
        assert response.body["detail"][0]["type"] == "json_invalid"
        assert response.body["detail"][0]["loc"] == ["body"]

    def test_a_json_array_body_is_422(self, daemon) -> None:
        response = daemon.client.post_evaluate([1, 2, 3])
        assert response.status == 422

    def test_missing_fields_are_reported_together(self, daemon) -> None:
        response = daemon.client.post_evaluate({})
        assert response.status == 422
        assert [entry["loc"] for entry in response.body["detail"]] == [
            ["body", "state"],
            ["body", "model"],
            ["body", "questions"],
        ]

    def test_invalid_utf8_is_422(self, daemon) -> None:
        response = daemon.client.request("POST", "/v1/systemone", raw_body=b"\xff\xfe\x00bad")
        assert response.status == 422

    def test_the_422_body_matches_the_pinned_shape(self, daemon) -> None:
        response = daemon.client.post_evaluate({"state": "s", "model": "m", "questions": {}})
        assert response.status == 422
        assert isinstance(response.body["detail"], list)
        for entry in response.body["detail"]:
            assert set(entry) >= {"loc", "msg", "type"}


class TestUnknownModel:
    def test_rejects_by_default(self, daemon) -> None:
        body = dict(MIXED_REQUEST, model="gpt-4o-mini")
        response = daemon.client.post_evaluate(body)
        assert response.status == 422
        assert response.body["detail"][0]["loc"] == ["body", "model"]
        assert response.body["detail"][0]["type"] == "model_not_found"

    def test_accepts_when_configured_to(self) -> None:
        with start_daemon(
            JEVMULATOR_PROVIDER="fake", JEVMULATOR_UNKNOWN_MODEL="accept"
        ) as running:
            response = running.client.post_evaluate(dict(MIXED_REQUEST, model="gpt-4o-mini"))
            assert response.status == 200
            assert response.body["model"] == "jevmulator-0.1.0-glm-5.3-flash"

    @pytest.mark.parametrize(
        "name", ["jev-latest", "jev-preview", "jevmulator-latest", "jevmulator-0.1.0-glm-5.3-flash"]
    )
    def test_every_listed_name_is_accepted(self, daemon, name: str) -> None:
        response = daemon.client.post_evaluate(dict(MIXED_REQUEST, model=name))
        assert response.status == 200, response.body

    def test_the_reported_model_is_always_the_resolved_identity(self, daemon) -> None:
        for name in ("jev-latest", "jev-preview", "jevmulator-latest"):
            response = daemon.client.post_evaluate(dict(MIXED_REQUEST, model=name))
            assert response.body["model"] == "jevmulator-0.1.0-glm-5.3-flash"


class TestRouting:
    def test_an_unknown_path_is_404(self, daemon) -> None:
        response = daemon.client.request("GET", "/v1/unknown")
        assert response.status == 404
        assert response.body["detail"]["error_type"] == "not_found"

    def test_get_on_the_evaluate_route_is_405(self, daemon) -> None:
        response = daemon.client.request("GET", "/v1/systemone")
        assert response.status == 405

    def test_a_query_string_does_not_break_routing(self, daemon) -> None:
        response = daemon.client.request("GET", "/v1/models?verbose=1")
        assert response.status == 200

    def test_the_debug_route_is_absent_unless_enabled(self) -> None:
        """With a valid key and recording off, the route is simply not there."""
        with start_daemon(JEVMULATOR_PROVIDER="fake") as running:
            response = running.client.request("GET", "/_jevmulator/debug/upstream-calls")
            assert response.status == 404
            assert response.body["detail"]["error_type"] == "not_found"


class TestBodyLimits:
    def test_a_body_over_the_limit_is_413(self) -> None:
        with start_daemon(JEVMULATOR_PROVIDER="fake", JEVMULATOR_MAX_BODY_BYTES="2048") as running:
            body = dict(MIXED_REQUEST, state="x" * 5000)
            response = running.client.post_evaluate(body)
            assert response.status == 413
            assert response.body["detail"]["error_type"] == "request_too_large"

    def test_a_large_body_under_the_limit_is_accepted(self, daemon) -> None:
        body = dict(MIXED_REQUEST, state="x" * 200_000)
        response = daemon.client.post_evaluate(body)
        assert response.status == 200

    def test_the_character_guard_reports_max_tokens_exceeded(self) -> None:
        with start_daemon(
            JEVMULATOR_PROVIDER="fake", JEVMULATOR_MAX_STATE_CHARS="100"
        ) as running:
            response = running.client.post_evaluate(dict(MIXED_REQUEST, state="y" * 500))
            assert response.status == 422
            assert response.body["detail"]["error_type"] == "max_tokens_exceeded"

    def test_the_character_guard_is_off_by_default(self, daemon) -> None:
        response = daemon.client.post_evaluate(dict(MIXED_REQUEST, state="y" * 50_000))
        assert response.status == 200


class TestHealthAndStatus:
    def test_health_reports_ready_with_the_fake_provider(self, daemon) -> None:
        body = daemon.client.health().body
        assert body["status"] == "ok"
        assert body["ready"] is True
        assert body["upstream_model"] == "glm-5.3-flash"

    def test_health_reports_degraded_without_an_upstream_credential(self) -> None:
        with start_daemon(
            JEVMULATOR_PROVIDER="openai",
            JEVMULATOR_UPSTREAM_API_KEY_ENV="A_VARIABLE_THAT_IS_NOT_SET",
        ) as running:
            body = running.client.health().body
            assert body["ready"] is False
            assert body["status"] == "degraded"
            assert any("A_VARIABLE_THAT_IS_NOT_SET" in problem for problem in body["problems"])

    def test_status_never_reveals_a_key(self, daemon) -> None:
        """The status body names the key variable and whether a key is present, never a value."""
        body = daemon.client.status().body
        raw = json.dumps(body)
        assert TEST_API_KEY not in raw
        config = body["config"]
        assert config["upstream_api_key_env"] == "ZAI_API_KEY"
        assert config["daemon_api_key_present"] is True
        assert "api_key" not in config
        assert "upstream_api_key" not in config

    def test_status_reports_the_whole_configuration(self, daemon) -> None:
        config = daemon.client.status().body["config"]
        for key in (
            "provider",
            "upstream_base_url",
            "upstream_model",
            "upstream_api_key_env",
            "upstream_api_key_present",
            "response_format",
            "usage_policy",
            "max_upstream_concurrency",
            "accepted_models",
        ):
            assert key in config

    def test_status_counts_requests(self, daemon) -> None:
        before = daemon.client.status().body["metrics"]["requests_total"]
        daemon.client.post_evaluate(MIXED_REQUEST)
        after = daemon.client.status().body["metrics"]["requests_total"]
        assert after == before + 1


class TestResponseHeaders:
    def test_the_upstream_model_is_reported_in_a_header(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        assert response.headers["X-Jevmulator-Upstream-Model"] == "glm-5.3-flash"

    def test_one_upstream_call_is_made_per_question(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        assert response.headers["X-Jevmulator-Upstream-Calls"] == "3"

    def test_the_usage_source_header_says_upstream(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        assert response.headers["X-Jevmulator-Usage-Source"] == "upstream"

    def test_a_request_id_header_is_present(self, daemon) -> None:
        """Both official SDKs look for x-typesafe-request-id on responses."""
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        assert response.headers["x-typesafe-request-id"]


class TestUsageAccounting:
    def test_usage_is_the_sum_of_the_upstream_counts(self, daemon) -> None:
        response = daemon.client.post_evaluate(MIXED_REQUEST)
        assert response.body["usage"]["input_tokens"] > 0
        assert response.body["usage"]["output_tokens"] > 0

    def test_a_provider_without_usage_is_marked_partial_not_invented(self) -> None:
        with start_daemon(JEVMULATOR_PROVIDER="fake", JEVMULATOR_FAKE_MODE="no_usage") as running:
            response = running.client.post_evaluate(MIXED_REQUEST)
            assert response.status == 200
            assert response.headers["X-Jevmulator-Usage-Source"] == "upstream-partial"
            assert response.body["usage"] == {"input_tokens": 0, "output_tokens": 0}

    def test_strict_usage_policy_fails_instead_of_reporting_an_unbacked_count(self) -> None:
        with start_daemon(
            JEVMULATOR_PROVIDER="fake",
            JEVMULATOR_FAKE_MODE="no_usage",
            JEVMULATOR_USAGE_POLICY="strict",
        ) as running:
            response = running.client.post_evaluate(MIXED_REQUEST)
            assert response.status == 502
            assert response.body["detail"]["error_type"] == "usage_unavailable"


class TestConcurrency:
    def test_many_simultaneous_requests_all_succeed(self, daemon) -> None:
        results: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            response = daemon.client.post_evaluate(MIXED_REQUEST)
            with lock:
                results.append(response.status)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert results == [200] * 12

    def test_the_inflight_limit_returns_429(self) -> None:
        with start_daemon(
            JEVMULATOR_PROVIDER="fake",
            JEVMULATOR_MAX_INFLIGHT_REQUESTS="1",
            JEVMULATOR_FAKE_DELAY_SECONDS="0.6",
            JEVMULATOR_MAX_UPSTREAM_CONCURRENCY="1",
        ) as running:
            statuses: list[int] = []
            lock = threading.Lock()

            def worker() -> None:
                response = running.client.post_evaluate(MIXED_REQUEST, timeout=60)
                with lock:
                    statuses.append(response.status)

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
            assert 429 in statuses
            for status in statuses:
                assert status in (200, 429)

    def test_the_inflight_counter_returns_to_zero(self, daemon) -> None:
        daemon.client.post_evaluate(MIXED_REQUEST)
        assert daemon.client.status().body["metrics"]["inflight"] == 0

    def test_a_failed_request_still_releases_its_slot(self) -> None:
        with start_daemon(
            JEVMULATOR_PROVIDER="fake", JEVMULATOR_FAKE_MODE="invalid_json"
        ) as running:
            for _ in range(3):
                assert running.client.post_evaluate(MIXED_REQUEST).status == 502
            assert running.client.status().body["metrics"]["inflight"] == 0
            assert running.client.status().body["metrics"]["requests_failed"] == 3


class TestResourceCleanup:
    def test_the_executor_and_provider_close_on_shutdown(self) -> None:
        with start_daemon(JEVMULATOR_PROVIDER="fake") as running:
            assert running.client.post_evaluate(MIXED_REQUEST).status == 200
            state = running.state
        assert state.executor._shutdown is True

    def test_the_port_is_released_after_shutdown(self) -> None:
        with start_daemon(JEVMULATOR_PROVIDER="fake") as running:
            port = running.port
        with pytest.raises(Exception):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/_jevmulator/health", timeout=2)

    def test_no_upstream_thread_survives_the_daemon(self) -> None:
        with start_daemon(JEVMULATOR_PROVIDER="fake") as running:
            running.client.post_evaluate(MIXED_REQUEST)
        remaining = [
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith("jevmulator-upstream")
        ]
        assert remaining == []


class TestClientDisconnect:
    """A client that drops its socket is ordinary, not a server fault."""

    def test_a_dropped_connection_does_not_kill_the_daemon(self, daemon) -> None:
        import socket

        for _ in range(3):
            raw = socket.create_connection(("127.0.0.1", daemon.port), timeout=5)
            raw.sendall(b"POST /v1/systemone HTTP/1.1\r\nHost: x\r\nContent-Length: 500\r\n\r\n")
            raw.close()

        assert daemon.client.post_evaluate(MIXED_REQUEST).status == 200

    def test_a_dropped_connection_is_not_reported_as_a_server_error(
        self, daemon, capsys
    ) -> None:
        import socket

        raw = socket.create_connection(("127.0.0.1", daemon.port), timeout=5)
        raw.sendall(b"GET /v1/models HTTP/1.1\r\nHost: x\r\n\r\n")
        raw.close()
        daemon.client.health()
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        assert "ConnectionResetError" not in captured.err


class TestDebugRouteRequiresAuth:
    """The recording holds caller state, instructions and whole prompts.

    An earlier version served it without any credential, so anyone who could reach the
    loopback port could read every prompt the daemon had sent upstream.
    """

    def test_get_without_a_key_is_401(self, daemon) -> None:
        response = daemon.client.request(
            "GET", "/_jevmulator/debug/upstream-calls", api_key=None
        )
        assert response.status == 401
        assert response.body["detail"]["error_type"] == "authentication_error"

    def test_get_with_a_wrong_key_is_401(self, daemon) -> None:
        response = daemon.client.request(
            "GET", "/_jevmulator/debug/upstream-calls", api_key="not-the-key"
        )
        assert response.status == 401

    def test_delete_without_a_key_is_401(self, daemon) -> None:
        response = daemon.client.request(
            "DELETE", "/_jevmulator/debug/upstream-calls", api_key=None
        )
        assert response.status == 401

    def test_an_unauthenticated_reader_never_sees_a_prompt(self, daemon) -> None:
        daemon.client.post_evaluate(MIXED_REQUEST)
        response = daemon.client.request(
            "GET", "/_jevmulator/debug/upstream-calls", api_key=None
        )
        assert response.status == 401
        assert "Duplicate charge" not in json.dumps(response.body)

    def test_the_authenticated_reader_does_see_them(self, daemon) -> None:
        daemon.client.post_evaluate(MIXED_REQUEST)
        calls = daemon.client.recorded_calls()
        assert calls
        assert "Duplicate charge" in json.dumps(calls)

    def test_auth_is_checked_before_the_recording_flag(self) -> None:
        """An unauthenticated caller learns nothing, not even whether recording is on."""
        with start_daemon(JEVMULATOR_PROVIDER="fake", JEVMULATOR_DEBUG_RECORD="0") as running:
            response = running.client.request(
                "GET", "/_jevmulator/debug/upstream-calls", api_key=None
            )
            assert response.status == 401

    def test_health_and_status_stay_open(self, daemon) -> None:
        """Readiness polling must work before a caller holds a key."""
        assert daemon.client.request("GET", "/_jevmulator/health", api_key=None).status == 200
        assert daemon.client.request("GET", "/_jevmulator/status", api_key=None).status == 200


class TestIncompleteRequestBody:
    """A client that declares a body and never finishes must not hold a handler forever."""

    def _send_partial(self, port: int, declared: int, sent: bytes, timeout: float):
        import socket

        raw = socket.create_connection(("127.0.0.1", port), timeout=timeout + 10)
        raw.sendall(
            b"POST /v1/systemone HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer " + TEST_API_KEY.encode() + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(declared).encode() + b"\r\n"
            b"\r\n" + sent
        )
        return raw

    def test_an_incomplete_body_is_answered_with_408(self) -> None:
        with start_daemon(
            JEVMULATOR_PROVIDER="fake", JEVMULATOR_INBOUND_TIMEOUT_SECONDS="1"
        ) as running:
            raw = self._send_partial(running.port, 5000, b'{"model": "jev-la', timeout=1.0)
            try:
                raw.settimeout(20)
                received = b""
                while b"\r\n\r\n" not in received:
                    chunk = raw.recv(4096)
                    if not chunk:
                        break
                    received += chunk
            finally:
                raw.close()
            assert b"408" in received.split(b"\r\n", 1)[0], received[:200]
            assert b"request_timeout" in received

    def test_the_daemon_serves_a_healthy_request_afterwards(self) -> None:
        with start_daemon(
            JEVMULATOR_PROVIDER="fake", JEVMULATOR_INBOUND_TIMEOUT_SECONDS="1"
        ) as running:
            raw = self._send_partial(running.port, 5000, b'{"model": "jev-la', timeout=1.0)
            try:
                raw.settimeout(20)
                raw.recv(4096)
            finally:
                raw.close()

            response = running.client.post_evaluate(MIXED_REQUEST)
            assert response.status == 200
            assert running.client.status().body["metrics"]["inflight"] == 0

    def test_a_stalled_client_does_not_consume_the_inflight_budget(self) -> None:
        """The stalled request never reaches the evaluator, so no slot is taken."""
        with start_daemon(
            JEVMULATOR_PROVIDER="fake",
            JEVMULATOR_INBOUND_TIMEOUT_SECONDS="1",
            JEVMULATOR_MAX_INFLIGHT_REQUESTS="1",
        ) as running:
            raw = self._send_partial(running.port, 5000, b"{", timeout=1.0)
            try:
                assert running.client.post_evaluate(MIXED_REQUEST).status == 200
            finally:
                raw.close()

    def test_a_client_that_disconnects_mid_body_is_not_a_server_error(self) -> None:
        import socket

        with start_daemon(
            JEVMULATOR_PROVIDER="fake", JEVMULATOR_INBOUND_TIMEOUT_SECONDS="2"
        ) as running:
            raw = socket.create_connection(("127.0.0.1", running.port), timeout=5)
            raw.sendall(
                b"POST /v1/systemone HTTP/1.1\r\nHost: x\r\n"
                b"Content-Length: 4000\r\n\r\n" + b"x" * 10
            )
            raw.close()
            assert running.client.post_evaluate(MIXED_REQUEST).status == 200

    def test_the_inbound_timeout_is_reported_in_status(self, daemon) -> None:
        assert daemon.client.status().body["config"]["inbound_timeout_seconds"] == 30.0
