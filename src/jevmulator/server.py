"""The HTTP daemon.

Serves the two pinned routes and a separate operational namespace. Built on
``http.server.ThreadingHTTPServer`` so a clean clone needs no third-party package.
"""

from __future__ import annotations

import hmac
import json
import logging
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import CONTRACT_PIN_DATE, __version__, errors, wire
from .config import Config
from .evaluator import Evaluator
from .providers import build_provider
from .providers.base import UpstreamCall

LOGGER = logging.getLogger("jevmulator")

PINNED_EVALUATE_PATH = "/v1/systemone"
PINNED_MODELS_PATH = "/v1/models"
OPERATIONAL_PREFIX = "/_jevmulator/"

MAX_RECORDED_CALLS = 200

#: An unread request body up to this size is drained before an early error response.
#: Draining keeps the keep-alive connection usable. A larger body closes the connection
#: instead of being read only to be discarded.
MAX_DRAIN_BYTES = 1024 * 1024

#: Socket failures that mean the client went away. They are logged at debug level and
#: never raised as server errors.
CLIENT_DISCONNECTED = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)


class DaemonState:
    """Everything one running daemon owns."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.started_at = time.time()
        self.provider = build_provider(config)
        self.executor = ThreadPoolExecutor(
            max_workers=config.max_upstream_concurrency,
            thread_name_prefix="jevmulator-upstream",
        )
        self.evaluator = Evaluator(config, self.provider, self.executor)

        self._lock = threading.Lock()
        self._inflight = 0
        self.requests_total = 0
        self.requests_failed = 0
        self.responses_with_partial_usage = 0
        self.recorded_calls: deque[UpstreamCall] = deque(maxlen=MAX_RECORDED_CALLS)

        if config.debug_record:
            self.provider.set_recorder(self._record)

    def _record(self, call: UpstreamCall) -> None:
        with self._lock:
            self.recorded_calls.append(call)

    def clear_recorded(self) -> None:
        with self._lock:
            self.recorded_calls.clear()

    def recorded(self) -> list[dict[str, Any]]:
        with self._lock:
            calls = list(self.recorded_calls)
        return [
            {
                "model": call.model,
                "messages": call.messages,
                "response_format": call.response_format,
                "schema": call.schema,
            }
            for call in calls
        ]

    def acquire_slot(self) -> bool:
        with self._lock:
            if self._inflight >= self.config.max_inflight_requests:
                return False
            self._inflight += 1
            return True

    def release_slot(self) -> None:
        with self._lock:
            self._inflight -= 1

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    def note_request(self, *, failed: bool, partial_usage: bool) -> None:
        with self._lock:
            self.requests_total += 1
            if failed:
                self.requests_failed += 1
            if partial_usage:
                self.responses_with_partial_usage += 1

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "requests_total": self.requests_total,
                "requests_failed": self.requests_failed,
                "responses_with_partial_usage": self.responses_with_partial_usage,
                "inflight": self._inflight,
                "recorded_upstream_calls": len(self.recorded_calls),
                "uptime_seconds": round(time.time() - self.started_at, 3),
            }

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.provider.close()


class Handler(BaseHTTPRequestHandler):
    """One request. ``server.state`` holds the daemon."""

    server_version = f"jevmulator/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing --------------------------------------------------------

    @property
    def state(self) -> DaemonState:
        return self.server.state  # type: ignore[attr-defined]

    def handle_one_request(self) -> None:
        self._body_consumed = False
        try:
            super().handle_one_request()
        except CLIENT_DISCONNECTED:
            # A client that drops its keep-alive socket is ordinary, not a fault. Node's
            # fetch agent and Python's urllib both do it at exit.
            self.close_connection = True

    def _drain_request_body(self) -> None:
        """Read and discard an unread request body before an early error response.

        A keep-alive connection breaks when the server answers while the client is still
        writing its body, which Windows reports as WinError 10053 or 10054. Draining keeps
        the connection usable for the next request. A body above ``MAX_DRAIN_BYTES`` is not
        read at all; the connection is closed instead.
        """
        if getattr(self, "_body_consumed", True):
            return
        self._body_consumed = True
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return
        try:
            length = int(raw_length)
        except ValueError:
            self.close_connection = True
            return
        if length <= 0:
            return
        if length > MAX_DRAIN_BYTES:
            self.close_connection = True
            return
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        LOGGER.info("%s - %s", self.address_string(), format % args)

    def _send_json(
        self, status: int, payload: Any, *, headers: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._drain_request_body()
        self.send_response(status)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Jevmulator-Version", __version__)
        self.send_header("x-typesafe-request-id", f"jevmulator-{time.time_ns():x}")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_error_object(self, error: errors.JevmulatorError) -> None:
        self._send_json(error.status, error.body(), headers=error.headers)

    # -- routing ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        path = self.path.split("?", 1)[0]
        try:
            if path == PINNED_MODELS_PATH:
                self._require_auth()
                self._send_json(200, wire.model_metadata_list(self.state.config.model_catalogue()))
                return
            if path == OPERATIONAL_PREFIX + "health":
                self._send_json(200, self._health())
                return
            if path == OPERATIONAL_PREFIX + "status":
                self._send_json(200, self._status())
                return
            if path == OPERATIONAL_PREFIX + "debug/upstream-calls":
                if not self.state.config.debug_record:
                    raise errors.NotFoundError(
                        "Upstream call recording is off. Set JEVMULATOR_DEBUG_RECORD=1 "
                        "to enable this route."
                    )
                self._send_json(200, {"calls": self.state.recorded()})
                return
            if path == PINNED_EVALUATE_PATH:
                raise errors.MethodNotAllowedError(
                    f"{PINNED_EVALUATE_PATH} accepts POST."
                )
            raise errors.NotFoundError(f"No route matches GET {path}.")
        except errors.JevmulatorError as error:
            self._send_error_object(error)

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if path == OPERATIONAL_PREFIX + "debug/upstream-calls":
                if not self.state.config.debug_record:
                    raise errors.NotFoundError(
                        "Upstream call recording is off. Set JEVMULATOR_DEBUG_RECORD=1 "
                        "to enable this route."
                    )
                self.state.clear_recorded()
                self._send_json(200, {"cleared": True})
                return
            raise errors.NotFoundError(f"No route matches DELETE {path}.")
        except errors.JevmulatorError as error:
            self._send_error_object(error)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if path != PINNED_EVALUATE_PATH:
                if path == PINNED_MODELS_PATH:
                    raise errors.MethodNotAllowedError(f"{PINNED_MODELS_PATH} accepts GET.")
                raise errors.NotFoundError(f"No route matches POST {path}.")
            self._handle_evaluate()
        except errors.JevmulatorError as error:
            self._send_error_object(error)

    # -- handlers --------------------------------------------------------

    def _health(self) -> dict[str, Any]:
        config = self.state.config
        ready = True
        problems: list[str] = []
        if config.provider == "openai" and not config.upstream_api_key:
            ready = False
            problems.append(
                f"No upstream credential: {config.upstream_api_key_env} is not set."
            )
        return {
            "status": "ok" if ready else "degraded",
            "ready": ready,
            "problems": problems,
            "version": __version__,
            "contract_pin_date": CONTRACT_PIN_DATE,
            "provider": config.provider,
            "upstream_model": config.upstream_model,
            "resolved_model_name": config.resolved_model_name,
            "port": config.port,
        }

    def _status(self) -> dict[str, Any]:
        return {
            "config": self.state.config.public_status(),
            "metrics": self.state.metrics(),
            "health": self._health(),
        }

    def _require_auth(self) -> None:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise errors.UnauthorizedError()
        token = header[len("Bearer ") :].strip()
        if not hmac.compare_digest(token, self.state.config.api_key):
            raise errors.UnauthorizedError()

    def _read_body(self) -> bytes:
        config = self.state.config
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise errors.RequestValidationError(
                [
                    errors.validation_detail(
                        ["header", "content-length"], "Field required", "missing"
                    )
                ]
            )
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise errors.RequestTooLargeError("Content-Length was not an integer.") from exc
        if length < 0:
            raise errors.RequestTooLargeError("Content-Length was negative.")
        if length > config.max_body_bytes:
            raise errors.RequestTooLargeError(
                f"The request body is {length} bytes, above the "
                f"{config.max_body_bytes} byte limit."
            )
        self._body_consumed = True
        return self.rfile.read(length)

    def _size_guard(self, payload: dict[str, Any], request: wire.SystemOneRequest) -> None:
        """Optional approximate size guard.

        It counts characters of the serialized content, not TypeSafe tokens. TypeSafe's
        tokenizer is unverified, so the guard is off by default.
        """
        config = self.state.config
        if config.max_request_chars:
            total = len(json.dumps(payload, ensure_ascii=False))
            if total > config.max_request_chars:
                raise errors.MaxTokensExceededError(
                    f"The request is {total} characters, above the "
                    f"{config.max_request_chars} character guard.",
                    ["body"],
                )
        if config.max_state_chars:
            state_size = len(json.dumps(request.state, ensure_ascii=False))
            if state_size > config.max_state_chars:
                raise errors.MaxTokensExceededError(
                    f"The state is {state_size} characters, above the "
                    f"{config.max_state_chars} character guard.",
                    ["body", "state"],
                )

    def _handle_evaluate(self) -> None:
        self._require_auth()
        body = self._read_body()

        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise errors.RequestValidationError(
                [
                    errors.validation_detail(
                        ["body"],
                        f"JSON decode error: {exc}",
                        "json_invalid",
                    )
                ]
            ) from exc

        request = wire.parse_request(payload)
        self._size_guard(payload, request)

        config = self.state.config
        if config.unknown_model == "reject" and request.model not in config.accepted_models:
            raise errors.RequestValidationError(
                [
                    errors.validation_detail(
                        ["body", "model"],
                        "Unknown model. Available names are returned by GET /v1/models: "
                        + ", ".join(config.accepted_models),
                        "model_not_found",
                        input_=request.model,
                    )
                ]
            )

        if not self.state.acquire_slot():
            raise errors.TooManyRequestsError(
                f"This daemon is already handling {config.max_inflight_requests} requests."
            )

        failed = True
        partial = False
        try:
            result = self.state.evaluator.evaluate(request)
            partial = not result.usage.complete
            response = wire.system_one_response(
                config.resolved_model_name,
                result.answers,
                result.usage.input_tokens,
                result.usage.output_tokens,
            )
            failed = False
            self._send_json(
                200,
                response,
                headers={
                    "X-Jevmulator-Upstream-Model": config.upstream_model,
                    "X-Jevmulator-Upstream-Calls": str(result.usage.calls),
                    "X-Jevmulator-Usage-Source": (
                        "upstream" if result.usage.complete else "upstream-partial"
                    ),
                },
            )
        finally:
            self.state.release_slot()
            self.state.note_request(failed=failed, partial_usage=partial)


class JevmulatorServer(ThreadingHTTPServer):
    """Threading HTTP server that carries the daemon state."""

    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 64

    def __init__(self, config: Config) -> None:
        self.state = DaemonState(config)
        super().__init__((config.host, config.port), Handler)

    def handle_error(self, request, client_address) -> None:
        """Do not print a traceback when the client simply went away."""
        error = sys.exc_info()[1]
        if isinstance(error, CLIENT_DISCONNECTED):
            LOGGER.debug("client %s closed the connection", client_address)
            return
        super().handle_error(request, client_address)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self.state.close()


def create_server(config: Config) -> JevmulatorServer:
    """Bind the daemon. The caller runs ``serve_forever`` and closes it."""
    return JevmulatorServer(config)
