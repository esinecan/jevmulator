"""The harness interface, and the pieces every adapter shares."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..jobs import RunJob
from ..profiles import Profile

#: System variables a harness child may inherit. Everything else is left out: the child
#: environment is built from this list, never by subtracting from the daemon's own. None of
#: them holds a secret. Programs locate themselves through them: pi finds Git Bash only as
#: ``%ProgramFiles%\Git\bin\bash.exe``, so without ``ProgramFiles`` a shell profile had no
#: shell at all (hand test H6, 2026-09-25).
SYSTEM_VARIABLES_WINDOWS = (
    "SystemRoot",
    "SystemDrive",
    "windir",
    "ComSpec",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "USERNAME",
    "USERDOMAIN",
    "COMPUTERNAME",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramW6432",
    "ProgramData",
    "CommonProgramFiles",
    "CommonProgramFiles(x86)",
    "CommonProgramW6432",
    "ALLUSERSPROFILE",
    "PUBLIC",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
)
SYSTEM_VARIABLES_POSIX = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "SHELL")


@dataclass(frozen=True)
class LaunchSpec:
    """Everything a harness needs to start one run's agent."""

    run_id: str
    run_dir: Path
    work_dir: Path
    session_dir: Path
    brief_path: Path
    events_path: Path
    stderr_path: Path
    tools: tuple[str, ...]
    read_roots: tuple[str, ...] | None
    settings: dict[str, Any]
    submit_url: str
    hello_url: str
    token: str
    max_nudges: int


class HarnessProcess:
    """A launched agent: its job, its root process and its event file."""

    def __init__(self, job: RunJob, popen: subprocess.Popen, spec: LaunchSpec) -> None:
        self.job = job
        self.popen = popen
        self.spec = spec

    @property
    def pid(self) -> int:
        return self.popen.pid

    def poll(self) -> int | None:
        return self.popen.poll()


class Harness:
    """Base class. An adapter implements every method."""

    name = "base"

    def problems(self) -> list[str]:
        """Why this harness cannot start a run, found once at startup. Empty when ready."""
        raise NotImplementedError

    def model_id(self, profile: Profile) -> str:
        """The ``provider/model`` the profile asks for, as the harness reports it."""
        raise NotImplementedError

    def tool_names(self, profile: Profile) -> list[str]:
        """The tool names the agent must be offered, ``submit_verdict`` included."""
        raise NotImplementedError

    def launch(self, spec: LaunchSpec) -> HarnessProcess:
        raise NotImplementedError


def child_environment(extra: dict[str, str], passthrough: tuple[str, ...] = ()) -> dict[str, str]:
    """Build a child environment from the allowlist, plus ``extra``.

    ``passthrough`` names further variables to copy when set, such as a provider key.
    """
    names = SYSTEM_VARIABLES_WINDOWS if os.name == "nt" else SYSTEM_VARIABLES_POSIX
    env: dict[str, str] = {}
    for name in names + tuple(passthrough):
        value = os.environ.get(name)
        if value:
            env[name] = value
    env.update(extra)
    return env


def run_variables(spec: LaunchSpec, *, replace_bash: bool) -> dict[str, str]:
    """The variables that tell an agent's tools where the form is."""
    import json

    return {
        "SYS1_SUBMIT_URL": spec.submit_url,
        "SYS1_HELLO_URL": spec.hello_url,
        "SYS1_RUN_TOKEN": spec.token,
        "SYS1_MAX_NUDGES": str(spec.max_nudges),
        "SYS1_WORK_DIR": str(spec.work_dir),
        "SYS1_READ_ROOTS": json.dumps(list(spec.read_roots or [])),
        "SYS1_REPLACE_BASH": "1" if replace_bash else "0",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "GIT_CEILING_DIRECTORIES": str(spec.run_dir),
    }
