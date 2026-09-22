"""Daemon configuration, read from the environment with documented defaults.

Two credentials exist and never mix. ``api_key`` authenticates callers of this daemon.
``upstream_api_key`` authenticates this daemon to the upstream provider. Neither value is
logged, printed, or included in any status body.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from typing import Any

from . import CONTRACT_PIN_DATE, __version__

DEFAULT_PORT = 8769
DEFAULT_HOST = "127.0.0.1"
DEFAULT_UPSTREAM_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
DEFAULT_UPSTREAM_MODEL = "glm-5.3-flash"
DEFAULT_UPSTREAM_API_KEY_ENV = "ZAI_API_KEY"

COMPATIBILITY_ALIASES = ("jev-latest", "jev-preview")
NATIVE_ALIAS = "jevmulator-latest"

PROVIDERS = ("openai", "fake")
RESPONSE_FORMATS = ("json_schema", "json_object", "none")
THINKING_MODES = ("disabled", "enabled", "omit")
USAGE_POLICIES = ("upstream", "strict")
UNKNOWN_MODEL_POLICIES = ("reject", "accept")


class ConfigError(ValueError):
    """The environment holds a value this daemon cannot act on."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean value, got {raw!r}")


def _env_choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = _env(name, default)
    assert value is not None
    if value not in allowed:
        raise ConfigError(f"{name} must be one of {', '.join(allowed)}, got {value!r}")
    return value


@dataclass(frozen=True)
class Config:
    """Every knob the daemon reads, resolved once at startup."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    api_key: str = ""
    api_key_generated: bool = False

    provider: str = "openai"
    upstream_base_url: str = DEFAULT_UPSTREAM_BASE_URL
    upstream_model: str = DEFAULT_UPSTREAM_MODEL
    upstream_api_key: str = ""
    upstream_api_key_env: str = DEFAULT_UPSTREAM_API_KEY_ENV

    response_format: str = "json_schema"
    thinking: str = "disabled"
    temperature: float | None = 0.0
    max_output_tokens: int = 2048

    upstream_timeout_seconds: float = 60.0
    request_timeout_seconds: float = 120.0
    inbound_timeout_seconds: float = 30.0
    upstream_retries: int = 2
    repair_retries: int = 1
    retry_backoff_seconds: float = 0.5
    retry_backoff_max_seconds: float = 8.0

    max_upstream_concurrency: int = 4
    max_inflight_requests: int = 16

    normalize_probabilities: bool = True
    probability_tolerance: float = 1e-6
    unknown_model: str = "reject"
    usage_policy: str = "upstream"

    max_body_bytes: int = 8 * 1024 * 1024
    max_state_chars: int = 0
    max_request_chars: int = 0

    debug_record: bool = False
    log_level: str = "INFO"

    extra: dict[str, Any] = field(default_factory=dict)

    # -- derived identity ------------------------------------------------

    @property
    def resolved_model_name(self) -> str:
        """The versioned identity reported in every successful response."""
        return f"jevmulator-{__version__}-{self.upstream_model}"

    @property
    def accepted_models(self) -> tuple[str, ...]:
        return COMPATIBILITY_ALIASES + (NATIVE_ALIAS, self.resolved_model_name)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def model_catalogue(self) -> list[dict[str, str]]:
        """The body of GET /v1/models.

        Every description names the upstream model, so no caller can mistake a
        Jevmulator answer for a TypeSafe answer.
        """
        backend = f"upstream model {self.upstream_model} via {self.provider}"
        return [
            {
                "name": COMPATIBILITY_ALIASES[0],
                "description": (
                    "Compatibility alias accepted for drop-in TypeSafe clients. "
                    f"Answered by {backend}, not by TypeSafe weights."
                ),
                "release_date": CONTRACT_PIN_DATE,
            },
            {
                "name": COMPATIBILITY_ALIASES[1],
                "description": (
                    "Compatibility alias accepted for drop-in TypeSafe clients. "
                    f"Answered by {backend}, not by TypeSafe weights."
                ),
                "release_date": CONTRACT_PIN_DATE,
            },
            {
                "name": NATIVE_ALIAS,
                "description": (
                    "Native Jevmulator alias that resolves to this daemon's current "
                    f"configuration. Answered by {backend}, not by TypeSafe weights."
                ),
                "release_date": CONTRACT_PIN_DATE,
            },
            {
                "name": self.resolved_model_name,
                "description": (
                    f"Jevmulator {__version__} emulating the TypeSafe Jev wire surface "
                    f"pinned on {CONTRACT_PIN_DATE}. Answered by {backend}, not by "
                    "TypeSafe weights."
                ),
                "release_date": CONTRACT_PIN_DATE,
            },
        ]

    def public_status(self) -> dict[str, Any]:
        """Configuration summary for the operational route. Holds no secret."""
        return {
            "version": __version__,
            "contract_pin_date": CONTRACT_PIN_DATE,
            "host": self.host,
            "port": self.port,
            "base_url": self.base_url,
            "provider": self.provider,
            "upstream_base_url": self.upstream_base_url,
            "upstream_model": self.upstream_model,
            "upstream_api_key_env": self.upstream_api_key_env,
            "upstream_api_key_present": bool(self.upstream_api_key),
            "daemon_api_key_present": bool(self.api_key),
            "daemon_api_key_generated": self.api_key_generated,
            "response_format": self.response_format,
            "thinking": self.thinking,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "upstream_timeout_seconds": self.upstream_timeout_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "inbound_timeout_seconds": self.inbound_timeout_seconds,
            "upstream_retries": self.upstream_retries,
            "repair_retries": self.repair_retries,
            "max_upstream_concurrency": self.max_upstream_concurrency,
            "max_inflight_requests": self.max_inflight_requests,
            "normalize_probabilities": self.normalize_probabilities,
            "probability_tolerance": self.probability_tolerance,
            "unknown_model": self.unknown_model,
            "usage_policy": self.usage_policy,
            "max_body_bytes": self.max_body_bytes,
            "max_state_chars": self.max_state_chars,
            "max_request_chars": self.max_request_chars,
            "debug_record": self.debug_record,
            "resolved_model_name": self.resolved_model_name,
            "accepted_models": list(self.accepted_models),
        }


def load_dotenv(path: str) -> dict[str, str]:
    """Read a ``KEY=value`` file as UTF-8 and return its pairs.

    Values are not exported into ``os.environ`` by this function. The caller decides.
    Lines that are blank or start with ``#`` are skipped. A surrounding pair of single
    or double quotes is removed.
    """
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw = line.partition("=")
            key = key.strip()
            raw = raw.strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
                raw = raw[1:-1]
            if key:
                values[key] = raw
    return values


def apply_dotenv(path: str) -> list[str]:
    """Load a ``.env`` file into the environment without overwriting existing values.

    Returns the names, never the values, of the variables it set.
    """
    applied: list[str] = []
    for key, value in load_dotenv(path).items():
        if key not in os.environ:
            os.environ[key] = value
            applied.append(key)
    return applied


def config_from_env(*, port: int | None = None, host: str | None = None) -> Config:
    """Build a :class:`Config` from the process environment.

    Args:
        port: Overrides ``JEVMULATOR_PORT`` when given, for the command line.
        host: Overrides ``JEVMULATOR_HOST`` when given, for the command line.

    Raises:
        ConfigError: A variable holds a value this daemon cannot act on.
    """
    provider = _env_choice("JEVMULATOR_PROVIDER", "openai", PROVIDERS)

    api_key = _env("JEVMULATOR_API_KEY") or ""
    api_key_generated = False
    if not api_key:
        api_key = "jevm_" + secrets.token_urlsafe(32)
        api_key_generated = True

    upstream_api_key_env = _env(
        "JEVMULATOR_UPSTREAM_API_KEY_ENV", DEFAULT_UPSTREAM_API_KEY_ENV
    )
    assert upstream_api_key_env is not None
    upstream_api_key = os.environ.get(upstream_api_key_env, "") or (
        _env("JEVMULATOR_UPSTREAM_API_KEY") or ""
    )

    resolved_port = port if port is not None else _env_int("JEVMULATOR_PORT", DEFAULT_PORT, 1)
    if resolved_port > 65535:
        raise ConfigError(f"port must be at most 65535, got {resolved_port}")
    resolved_host = host if host is not None else _env("JEVMULATOR_HOST", DEFAULT_HOST)
    assert resolved_host is not None

    temperature_raw = _env("JEVMULATOR_UPSTREAM_TEMPERATURE", "0")
    temperature: float | None
    if temperature_raw is not None and temperature_raw.lower() == "omit":
        temperature = None
    else:
        temperature = _env_float("JEVMULATOR_UPSTREAM_TEMPERATURE", 0.0)

    config = Config(
        host=resolved_host,
        port=resolved_port,
        api_key=api_key,
        api_key_generated=api_key_generated,
        provider=provider,
        upstream_base_url=(
            _env("JEVMULATOR_UPSTREAM_BASE_URL", DEFAULT_UPSTREAM_BASE_URL) or ""
        ).rstrip("/"),
        upstream_model=_env("JEVMULATOR_UPSTREAM_MODEL", DEFAULT_UPSTREAM_MODEL) or "",
        upstream_api_key=upstream_api_key,
        upstream_api_key_env=upstream_api_key_env,
        response_format=_env_choice(
            "JEVMULATOR_RESPONSE_FORMAT", "json_schema", RESPONSE_FORMATS
        ),
        thinking=_env_choice("JEVMULATOR_UPSTREAM_THINKING", "disabled", THINKING_MODES),
        temperature=temperature,
        max_output_tokens=_env_int("JEVMULATOR_MAX_OUTPUT_TOKENS", 2048, 1),
        upstream_timeout_seconds=_env_float(
            "JEVMULATOR_UPSTREAM_TIMEOUT_SECONDS", 60.0, 0.001
        ),
        request_timeout_seconds=_env_float(
            "JEVMULATOR_REQUEST_TIMEOUT_SECONDS", 120.0, 0.001
        ),
        inbound_timeout_seconds=_env_float(
            "JEVMULATOR_INBOUND_TIMEOUT_SECONDS", 30.0, 0.001
        ),
        upstream_retries=_env_int("JEVMULATOR_UPSTREAM_RETRIES", 2, 0),
        repair_retries=_env_int("JEVMULATOR_REPAIR_RETRIES", 1, 0),
        retry_backoff_seconds=_env_float("JEVMULATOR_RETRY_BACKOFF_SECONDS", 0.5, 0.0),
        retry_backoff_max_seconds=_env_float(
            "JEVMULATOR_RETRY_BACKOFF_MAX_SECONDS", 8.0, 0.0
        ),
        max_upstream_concurrency=_env_int("JEVMULATOR_MAX_UPSTREAM_CONCURRENCY", 4, 1),
        max_inflight_requests=_env_int("JEVMULATOR_MAX_INFLIGHT_REQUESTS", 16, 1),
        normalize_probabilities=_env_bool("JEVMULATOR_NORMALIZE_PROBABILITIES", True),
        probability_tolerance=_env_float("JEVMULATOR_PROBABILITY_TOLERANCE", 1e-6, 0.0),
        unknown_model=_env_choice(
            "JEVMULATOR_UNKNOWN_MODEL", "reject", UNKNOWN_MODEL_POLICIES
        ),
        usage_policy=_env_choice("JEVMULATOR_USAGE_POLICY", "upstream", USAGE_POLICIES),
        max_body_bytes=_env_int("JEVMULATOR_MAX_BODY_BYTES", 8 * 1024 * 1024, 1),
        max_state_chars=_env_int("JEVMULATOR_MAX_STATE_CHARS", 0, 0),
        max_request_chars=_env_int("JEVMULATOR_MAX_REQUEST_CHARS", 0, 0),
        debug_record=_env_bool("JEVMULATOR_DEBUG_RECORD", False),
        log_level=(_env("JEVMULATOR_LOG_LEVEL", "INFO") or "INFO").upper(),
    )

    if config.provider == "openai" and not config.upstream_base_url:
        raise ConfigError("JEVMULATOR_UPSTREAM_BASE_URL must not be empty")
    if config.provider == "openai" and not config.upstream_model:
        raise ConfigError("JEVMULATOR_UPSTREAM_MODEL must not be empty")
    return config
