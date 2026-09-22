"""The runtime state file that the daemon and the lifecycle scriptlet share.

The file lives at ``<state dir>/runtime.json`` and is written by the daemon once it has
bound its socket. The scriptlet adds ``process_start_time`` after it has looked the
process up, so ``stop`` can verify process identity instead of trusting a bare process id.

All reads and writes use explicit UTF-8 without a byte order mark, so Windows PowerShell
5.1 and Python agree on the bytes.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

DEFAULT_STATE_DIR = ".jevmulator"
RUNTIME_FILE = "runtime.json"


def state_dir(base: str | None = None) -> str:
    """Directory holding the runtime file and the daemon logs."""
    if base:
        return base
    return os.environ.get("JEVMULATOR_STATE_DIR") or os.path.join(os.getcwd(), DEFAULT_STATE_DIR)


def runtime_path(base: str | None = None) -> str:
    return os.path.join(state_dir(base), RUNTIME_FILE)


def write_runtime(config, *, base: str | None = None, extra: dict[str, Any] | None = None) -> str:
    """Write the runtime file for a daemon that has just bound its socket."""
    directory = state_dir(base)
    os.makedirs(directory, exist_ok=True)
    payload: dict[str, Any] = {
        "pid": os.getpid(),
        "host": config.host,
        "port": config.port,
        "base_url": config.base_url,
        "health_url": config.base_url + "/_jevmulator/health",
        "api_key": config.api_key,
        "api_key_generated": config.api_key_generated,
        "provider": config.provider,
        "upstream_model": config.upstream_model,
        "resolved_model_name": config.resolved_model_name,
        "started_at": time.time(),
        "state_dir": directory,
    }
    if extra:
        payload.update(extra)
    path = os.path.join(directory, RUNTIME_FILE)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def read_runtime(base: str | None = None) -> dict[str, Any] | None:
    """Read the runtime file, or return ``None`` when it is missing or unreadable."""
    path = runtime_path(base)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def clear_runtime(base: str | None = None) -> bool:
    """Remove the runtime file. Returns whether a file was removed."""
    path = runtime_path(base)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False
