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
from .sys1.service import Sys1Service

LOGGER = logging.getLogger("jevmulator")

PINNED_EVALUATE_PATH = "/v1/systemone"
PINNED_MODELS_PATH = "/v1/models"
OPERATIONAL_PREFIX = "/_jevmulator/"

#: The harnessed mode mirrors the pinned layout under ``/sys1``, with the pinned bodies, so
#: an official SDK reaches it by changing only its base URL.
SYS1_EVALUATE_PATH = "/sys1" + PINNED_EVALUATE_PATH
SYS1_MODELS_PATH = "/sys1" + PINNED_MODELS_PATH
SYS1_RUNS_PREFIX = OPERATIONAL_PREFIX + "sys1/runs/"
SYS1_FORM_ACTIONS = ("submission", "hello")

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
        # First, so a bad sys1 profile stops startup before any thread exists.
        self.sys1 = Sys1Service(config)
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
        # sys1 first: its runs are marked cancelled, their waiting handlers are answered,
        # and their jobs are closed. Only then does the bare executor shut down, because
        # that call waits without a time limit.
        self.sys1.close()
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

    def setup(self) -> None:
        """Bound every read on this connection.

        Without a socket timeout, a client that opens a connection, declares a body and
        then stops sending occupies one handler thread for as long as it likes. The
        evaluation deadline never applies, because the request never reaches the
        evaluator.
        """
        super().setup()
        timeout = self.server.state.config.inbound_timeout_seconds  # type: ignore[attr-defined]
        if timeout > 0:
            self.connection.settimeout(timeout)

    def handle_one_request(self) -> None:
        self._body_consumed = False
        try:
            super().handle_one_request()
        except CLIENT_DISCONNECTED:
            # A client that drops its keep-alive socket is ordinary, not a fault. Node's
            # fetch agent and Python's urllib both do it at exit.
            self.close_connection = True
        except TimeoutError:
            # An idle keep-alive connection reached the inbound timeout. Close it quietly.
            self.close_connection = True

    def _read_bounded(self, length: int, deadline: float) -> bytes:
        """Read exactly ``length`` bytes, or raise at the absolute ``deadline``.

        ``rfile.read(n)`` is a buffered read that loops over several underlying receives
        until it has ``n`` bytes. Control never returns between them, so a deadline check
        placed around that call never runs, and a client that trickles bytes resets the
        per-socket timeout on every receive and holds the handler open indefinitely.

        This reads through ``read1``, which returns after one underlying receive, and sets
        the socket timeout to the remaining budget before each one. The total is therefore
        bounded by the deadline no matter how the client paces its bytes.
        """
        read_once = getattr(self.rfile, "read1", None) or self.rfile.read
        base_timeout = self.state.config.inbound_timeout_seconds
        chunks: list[bytes] = []
        remaining = length
        try:
            while remaining > 0:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    self.close_connection = True
                    raise errors.RequestTimeoutError(
                        "The request body was still incomplete after "
                        f"{base_timeout} seconds. "
                        f"{length - remaining} of {length} bytes arrived."
                    )
                try:
                    self.connection.settimeout(budget)
                except OSError:
                    pass
                try:
                    chunk = read_once(min(65536, remaining))
                except TimeoutError as exc:
                    self.close_connection = True
                    raise errors.RequestTimeoutError(
                        f"The request body stalled for {base_timeout} seconds. "
                        f"{length - remaining} of {length} bytes arrived."
                    ) from exc
                if not chunk:
                    self.close_connection = True
                    raise errors.RequestTimeoutError(
                        "The client closed the connection before the whole request body "
                        f"arrived. {length - remaining} of {length} bytes arrived."
                    )
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            # Restore the ordinary timeout, so the response write and the next keep-alive
            # request line do not inherit a nearly expired budget.
            try:
                self.connection.settimeout(base_timeout if base_timeout > 0 else None)
            except OSError:
                pass
        return b"".join(chunks)

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
        deadline = time.monotonic() + self.state.config.inbound_timeout_seconds
        try:
            self._read_bounded(length, deadline)
        except (errors.RequestTimeoutError, OSError):
            # The response is already being written, so there is nothing to report. The
            # connection closes rather than staying in an unknown state.
            self.close_connection = True

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
            if path == SYS1_MODELS_PATH:
                self._require_auth()
                self._send_json(200, wire.model_metadata_list(self.state.sys1.model_catalogue()))
                return
            if path == SYS1_EVALUATE_PATH:
                raise errors.MethodNotAllowedError(f"{SYS1_EVALUATE_PATH} accepts POST.")
            if path == OPERATIONAL_PREFIX + "health":
                self._send_json(200, self._health())
                return
            if path == OPERATIONAL_PREFIX + "status":
                self._send_json(200, self._status())
                return
            if path == OPERATIONAL_PREFIX + "debug/upstream-calls":
                # The recording holds caller state, instructions and whole prompts, so it
                # is at least as sensitive as the evaluate route and carries the same
                # bearer requirement.
                self._require_auth()
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
                self._require_auth()
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
            if path == SYS1_EVALUATE_PATH:
                self._handle_sys1_evaluate()
                return
            if path.startswith(SYS1_RUNS_PREFIX):
                run_id, _, action = path[len(SYS1_RUNS_PREFIX) :].partition("/")
                if action not in SYS1_FORM_ACTIONS:
                    raise errors.NotFoundError(f"No route matches POST {path}.")
                self._handle_sys1_form(run_id, action)
                return
            if path != PINNED_EVALUATE_PATH:
                if path in (PINNED_MODELS_PATH, SYS1_MODELS_PATH):
                    raise errors.MethodNotAllowedError(f"{path} accepts GET.")
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
            # sys1 readiness is reported apart from the bare path's. ``ready`` and
            # ``problems`` above stay about /v1/systemone, so jevmulator.ps1 behaves as before
            # on a machine without pi.
            "sys1": self.state.sys1.health(),
        }

    def _status(self) -> dict[str, Any]:
        metrics = self.state.metrics()
        metrics["sys1"] = self.state.sys1.metrics()
        return {
            "config": self.state.config.public_status(),
            "metrics": metrics,
            "health": self._health(),
        }

    def _require_auth(self) -> None:
        """Require the daemon bearer token.

        Health and status stay open, because readiness polling must work before a caller
        holds a key and neither route returns a secret or any caller content. The debug
        recording route is not open: it returns the prompts, which carry caller content.
        """
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

        # One absolute deadline covers the whole body, however the client paces it.
        self._body_consumed = True
        return self._read_bounded(length, time.monotonic() + config.inbound_timeout_seconds)

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

    def _decode_json(self, body: bytes) -> Any:
        try:
            return json.loads(body.decode("utf-8"))
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

    def _handle_evaluate(self) -> None:
        self._require_auth()
        body = self._read_body()
        payload = self._decode_json(body)

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

    def _handle_sys1_evaluate(self) -> None:
        """One request on /sys1: validated like the bare path, answered by an agent run.

        The handler never takes a bare in-flight slot. sys1 has its own run slots, so a
        run that takes minutes cannot starve /v1/systemone.
        """
        self._require_auth()
        body = self._read_body()
        payload = self._decode_json(body)
        request = wire.parse_request(payload)
        self._size_guard(payload, request)

        sys1 = self.state.sys1
        profile = sys1.resolve_profile(request.model)
        outcome, run_id, kind = sys1.evaluate(
            payload, request, profile, self.headers.get("X-TypeSafe-Retry-Count")
        )
        headers = dict(outcome.headers)
        headers["X-Jevmulator-Sys1-Run"] = run_id
        headers["X-Jevmulator-Sys1-Coalesced"] = kind
        self._send_json(outcome.status, outcome.body, headers=headers)

    def _handle_sys1_form(self, run_id: str, action: str) -> None:
        """A post from a running agent's harness, authenticated by the run's own token.

        The daemon key is not accepted here, and the run token is accepted nowhere else.
        """
        header = self.headers.get("Authorization", "")
        token = header[len("Bearer ") :].strip() if header.startswith("Bearer ") else ""
        run = self.state.sys1.authorized_run(run_id, token)
        body = self._read_body()
        payload = self._decode_json(body)
        result = run.submit(payload) if action == "submission" else run.hello(payload)
        self._send_json(200, result)


class JevmulatorServer(ThreadingHTTPServer):
    """Threading HTTP server that carries the daemon state."""

    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 64

    def __init__(self, config: Config) -> None:
        self.state = DaemonState(config)
        try:
            super().__init__((config.host, config.port), Handler)
        except OSError:
            self.state.close()
            raise
        self.state.sys1.set_address(self.server_address[0], self.server_address[1])

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
