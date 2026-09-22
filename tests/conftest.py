"""Shared fixtures.

No test reaches a live provider. The default provider is ``fake``, and the tests that
need a real socket use a controlled fake upstream HTTP server bound to loopback.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT_DIR = os.path.join(REPO_ROOT, "contract")
SCHEMA_DIR = os.path.join(CONTRACT_DIR, "schemas")
FIXTURE_DIR = os.path.join(CONTRACT_DIR, "fixtures")

TEST_API_KEY = "test-daemon-key"

#: Environment variables the daemon reads. Cleared before every test so one test's
#: configuration cannot leak into another.
JEVMULATOR_VARS = [
    "JEVMULATOR_API_KEY",
    "JEVMULATOR_PROVIDER",
    "JEVMULATOR_UPSTREAM_BASE_URL",
    "JEVMULATOR_UPSTREAM_MODEL",
    "JEVMULATOR_UPSTREAM_API_KEY",
    "JEVMULATOR_UPSTREAM_API_KEY_ENV",
    "JEVMULATOR_RESPONSE_FORMAT",
    "JEVMULATOR_UPSTREAM_THINKING",
    "JEVMULATOR_UPSTREAM_TEMPERATURE",
    "JEVMULATOR_MAX_OUTPUT_TOKENS",
    "JEVMULATOR_UPSTREAM_TIMEOUT_SECONDS",
    "JEVMULATOR_REQUEST_TIMEOUT_SECONDS",
    "JEVMULATOR_UPSTREAM_RETRIES",
    "JEVMULATOR_REPAIR_RETRIES",
    "JEVMULATOR_RETRY_BACKOFF_SECONDS",
    "JEVMULATOR_RETRY_BACKOFF_MAX_SECONDS",
    "JEVMULATOR_MAX_UPSTREAM_CONCURRENCY",
    "JEVMULATOR_MAX_INFLIGHT_REQUESTS",
    "JEVMULATOR_NORMALIZE_PROBABILITIES",
    "JEVMULATOR_PROBABILITY_TOLERANCE",
    "JEVMULATOR_UNKNOWN_MODEL",
    "JEVMULATOR_USAGE_POLICY",
    "JEVMULATOR_MAX_BODY_BYTES",
    "JEVMULATOR_MAX_STATE_CHARS",
    "JEVMULATOR_MAX_REQUEST_CHARS",
    "JEVMULATOR_DEBUG_RECORD",
    "JEVMULATOR_INBOUND_TIMEOUT_SECONDS",
    "JEVMULATOR_LOG_LEVEL",
    "JEVMULATOR_FAKE_MODE",
    "JEVMULATOR_FAKE_DELAY_SECONDS",
    "JEVMULATOR_STATE_DIR",
    "JEVMULATOR_PORT",
    "JEVMULATOR_HOST",
]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from a known configuration."""
    for name in JEVMULATOR_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JEVMULATOR_PROVIDER", "fake")
    monkeypatch.setenv("JEVMULATOR_API_KEY", TEST_API_KEY)


def free_port() -> int:
    """Ask the operating system for a port nobody is listening on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class HttpResponse:
    status: int
    body: Any
    headers: dict[str, str]


class DaemonClient:
    """Small HTTP client for the daemon under test."""

    def __init__(self, base_url: str, api_key: str = TEST_API_KEY) -> None:
        self.base_url = base_url
        self.api_key = api_key

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        api_key: str | None = "__default__",
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        request_headers = {"Accept": "application/json"}
        key = self.api_key if api_key == "__default__" else api_key
        if key is not None:
            request_headers["Authorization"] = "Bearer " + key
        data = raw_body
        if data is None and body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})

        request = urllib.request.Request(
            self.base_url + path, data=data, headers=request_headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8")
                return HttpResponse(
                    response.status, _maybe_json(payload), dict(response.headers)
                )
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8")
            return HttpResponse(exc.code, _maybe_json(payload), dict(exc.headers))

    def post_evaluate(self, body: Any, **kwargs: Any) -> HttpResponse:
        return self.request("POST", "/v1/systemone", body, **kwargs)

    def get_models(self, **kwargs: Any) -> HttpResponse:
        return self.request("GET", "/v1/models", **kwargs)

    def health(self) -> HttpResponse:
        return self.request("GET", "/_jevmulator/health", api_key=None)

    def status(self) -> HttpResponse:
        return self.request("GET", "/_jevmulator/status", api_key=None)

    def recorded_calls(self) -> list[dict[str, Any]]:
        """The recorded upstream payloads. This route requires the daemon bearer token."""
        response = self.request("GET", "/_jevmulator/debug/upstream-calls")
        assert response.status == 200, response.body
        return response.body["calls"]

    def clear_recorded(self) -> None:
        self.request("DELETE", "/_jevmulator/debug/upstream-calls")


def _maybe_json(payload: str) -> Any:
    try:
        return json.loads(payload)
    except ValueError:
        return payload


@dataclass
class RunningDaemon:
    client: DaemonClient
    server: Any
    port: int
    state: Any


def start_daemon(**env: str):
    """Start a daemon on an ephemeral port inside this process.

    Returns a context manager yielding :class:`RunningDaemon`.
    """
    from contextlib import contextmanager

    @contextmanager
    def _runner():
        from jevmulator.config import config_from_env
        from jevmulator.server import create_server

        # Set the key explicitly rather than relying on the autouse environment fixture.
        # A module-scoped fixture is built before function-scoped ones run, so an ambient
        # environment would otherwise let the daemon generate a key the client never sees.
        env.setdefault("JEVMULATOR_API_KEY", TEST_API_KEY)
        env.setdefault("JEVMULATOR_PROVIDER", "fake")

        previous = {name: os.environ.get(name) for name in env}
        os.environ.update(env)
        try:
            config = config_from_env(port=0, host="127.0.0.1")
            server = create_server(config)
            port = server.server_address[1]
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
            )
            thread.start()
            try:
                yield RunningDaemon(
                    client=DaemonClient(
                        f"http://127.0.0.1:{port}", env.get("JEVMULATOR_API_KEY", TEST_API_KEY)
                    ),
                    server=server,
                    port=port,
                    state=server.state,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    return _runner()


@pytest.fixture
def daemon():
    """A daemon backed by the deterministic fake provider, with recording on."""
    with start_daemon(JEVMULATOR_PROVIDER="fake", JEVMULATOR_DEBUG_RECORD="1") as running:
        yield running


# -- controlled fake upstream -------------------------------------------


@dataclass
class UpstreamScript:
    """What the fake upstream HTTP server does, call by call.

    ``responses`` is consumed in order; the last entry repeats once exhausted.
    """

    responses: list[dict[str, Any]] = field(default_factory=list)
    received: list[dict[str, Any]] = field(default_factory=list)
    delay_seconds: float = 0.0

    def next_response(self) -> dict[str, Any]:
        if not self.responses:
            return {"status": 200, "json": _chat_completion('{"answer": {"p_yes": 0.5}}')}
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


def _chat_completion(
    content: str, *, prompt_tokens: int | None = 31, completion_tokens: int | None = 7,
    finish_reason: str = "stop", refusal: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if refusal is not None:
        message["refusal"] = refusal
    payload: dict[str, Any] = {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": "fake-upstream-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if prompt_tokens is not None or completion_tokens is not None:
        payload["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": (prompt_tokens or 0) + (completion_tokens or 0),
        }
    return payload


def chat_completion(content: str, **kwargs: Any) -> dict[str, Any]:
    """Public helper so tests can build an upstream body."""
    return _chat_completion(content, **kwargs)


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # noqa: A002
        return

    def do_POST(self) -> None:  # noqa: N802
        script: UpstreamScript = self.server.script  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except ValueError:
            decoded = {"_undecodable": raw.decode("utf-8", "replace")}
        script.received.append(
            {"path": self.path, "body": decoded, "headers": dict(self.headers)}
        )

        if script.delay_seconds:
            import time as _time

            _time.sleep(script.delay_seconds)

        instruction = script.next_response()
        status = int(instruction.get("status", 200))
        headers = dict(instruction.get("headers", {}))

        if "raw" in instruction:
            body = instruction["raw"].encode("utf-8")
        else:
            body = json.dumps(instruction.get("json", {})).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


class FakeUpstream:
    """A real HTTP server that speaks the OpenAI Chat Completions shape."""

    def __init__(self, script: UpstreamScript) -> None:
        self.script = script
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
        self.server.script = script  # type: ignore[attr-defined]
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def call_count(self) -> int:
        return len(self.script.received)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


@pytest.fixture
def upstream_factory():
    """Build controlled fake upstreams and close them at the end of the test."""
    created: list[FakeUpstream] = []

    def _build(
        responses: list[dict[str, Any]] | None = None, delay_seconds: float = 0.0
    ) -> FakeUpstream:
        upstream = FakeUpstream(
            UpstreamScript(responses=list(responses or []), delay_seconds=delay_seconds)
        )
        created.append(upstream)
        return upstream

    yield _build
    for upstream in created:
        upstream.close()


def load_fixture(name: str) -> Any:
    with open(os.path.join(FIXTURE_DIR, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_schema(name: str) -> Any:
    with open(os.path.join(SCHEMA_DIR, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


#: A request touching all three primitives, structured content, and a null description.
MIXED_REQUEST: dict[str, Any] = {
    "model": "jev-latest",
    "state": {
        "subject": "Duplicate charge",
        "message": "I was charged twice for the same order. Please refund one.",
    },
    "questions": {
        "billing": {"type": "noul", "instructions": "Is this message about billing?"},
        "tone": {
            "type": "choice",
            "instructions": "What is the tone of this message?",
            "criteria": {
                "angry": "An upset or hostile message",
                "calm": None,
                "excited": {"note": "An enthusiastic or eager message"},
            },
        },
        "urgency": {
            "type": "score",
            "instructions": {"task": "How urgent is this message?"},
            "criteria": ["Can wait", "Needs attention this week", ["Needs", "attention", "today"]],
        },
    },
}
