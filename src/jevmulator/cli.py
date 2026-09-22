"""Command line entry point.

``jevmulator serve`` runs the daemon in the foreground. The PowerShell scriptlet starts
that command in a hidden background process, then waits for readiness.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import urllib.error
import urllib.request
from typing import Any

from . import __version__, runtime
from .config import ConfigError, apply_dotenv, config_from_env


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def _serve(args: argparse.Namespace) -> int:
    if args.env_file:
        apply_dotenv(args.env_file)
    elif os.path.exists(".env"):
        apply_dotenv(".env")

    try:
        config = config_from_env(port=args.port, host=args.host)
    except ConfigError as exc:
        print(f"jevmulator: configuration error: {exc}", file=sys.stderr)
        return 2

    _configure_logging(config.log_level)

    from .server import create_server  # imported late so config errors surface first

    try:
        server = create_server(config)
    except OSError as exc:
        print(
            f"jevmulator: cannot bind {config.host}:{config.port}: {exc}",
            file=sys.stderr,
        )
        return 3

    path = runtime.write_runtime(config, base=args.state_dir)
    print(
        f"jevmulator {__version__} listening on {config.base_url} "
        f"(provider {config.provider}, upstream model {config.upstream_model})",
        flush=True,
    )
    print(f"jevmulator: runtime file {path}", flush=True)

    stopping = threading.Event()

    def _shutdown(signum: int, _frame: Any) -> None:
        if stopping.is_set():
            return
        stopping.set()
        print(f"jevmulator: signal {signum} received, shutting down", flush=True)
        threading.Thread(target=server.shutdown, daemon=True).start()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signal_number = getattr(signal, name, None)
        if signal_number is not None:
            try:
                signal.signal(signal_number, _shutdown)
            except (ValueError, OSError):
                pass

    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runtime.clear_runtime(args.state_dir)
    print("jevmulator: stopped", flush=True)
    return 0


def _health(args: argparse.Namespace) -> int:
    record = runtime.read_runtime(args.state_dir)
    if args.url:
        url = args.url.rstrip("/") + "/_jevmulator/health"
    elif record:
        url = record["health_url"]
    else:
        print("jevmulator: no runtime file and no --url given", file=sys.stderr)
        return 4
    try:
        with urllib.request.urlopen(url, timeout=args.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"jevmulator: health check failed: {exc}", file=sys.stderr)
        return 5
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ready") else 6


def _status(args: argparse.Namespace) -> int:
    record = runtime.read_runtime(args.state_dir)
    if record is None:
        print(json.dumps({"running": False, "reason": "no runtime file"}, indent=2))
        return 1
    redacted = dict(record)
    redacted["api_key"] = "<present>" if record.get("api_key") else "<absent>"
    print(json.dumps({"running": True, "runtime": redacted}, ensure_ascii=False, indent=2))
    return 0


def _print_key(args: argparse.Namespace) -> int:
    record = runtime.read_runtime(args.state_dir)
    if record is None or not record.get("api_key"):
        print("jevmulator: no runtime file with an API key", file=sys.stderr)
        return 1
    print(record["api_key"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jevmulator",
        description=(
            "Local daemon serving the pinned TypeSafe Jev wire surface, backed by a "
            "configurable OpenAI-compatible model."
        ),
    )
    parser.add_argument("--version", action="version", version=f"jevmulator {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the daemon in the foreground")
    serve.add_argument("--port", type=int, default=None, help="TCP port, default 8769")
    serve.add_argument("--host", default=None, help="bind address, default 127.0.0.1")
    serve.add_argument("--env-file", default=None, help="read this .env file first")
    serve.add_argument("--state-dir", default=None, help="directory for the runtime file")
    serve.set_defaults(func=_serve)

    health = sub.add_parser("health", help="query a running daemon's health route")
    health.add_argument("--url", default=None, help="base URL, default from the runtime file")
    health.add_argument("--state-dir", default=None)
    health.add_argument("--timeout", type=float, default=5.0)
    health.set_defaults(func=_health)

    status = sub.add_parser("status", help="print the runtime file without the key")
    status.add_argument("--state-dir", default=None)
    status.set_defaults(func=_status)

    key = sub.add_parser("print-key", help="print the daemon API key from the runtime file")
    key.add_argument("--state-dir", default=None)
    key.set_defaults(func=_print_key)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
