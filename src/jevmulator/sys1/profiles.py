"""sys1 profiles: which tools an agent gets, and the brief it reads.

A profile is one JSON file plus one brief template, shipped inside the package under
``builtin_profiles``. The structural control over an agent lives here: a profile grants
tools, and a harness offers the agent nothing else.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import ConfigError

BUILTIN_DIR = Path(__file__).with_name("builtin_profiles")

#: Harness-neutral tool names. Each harness adapter maps them to its own tools.
ABSTRACT_TOOLS = ("read", "grep", "find", "ls", "shell", "write", "edit")

#: A profile holding any of these can change the machine, so it is opt-in.
SHELL_TOOLS = frozenset({"shell", "write", "edit"})

THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

MODEL_PREFIX = "sys1-"
DEFAULT_ALIASES = ("jev-latest", "jev-preview", "sys1-latest")

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class ProfileError(ConfigError):
    """A profile file cannot be used. Startup stops, as it does for a bad variable."""


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    tools: tuple[str, ...]
    brief_template: str
    workdir: str
    read_roots: tuple[str, ...] | None
    timeout_seconds: float
    harness: dict[str, dict[str, Any]]

    @property
    def is_shell(self) -> bool:
        return any(tool in SHELL_TOOLS for tool in self.tools)

    @property
    def model_name(self) -> str:
        return MODEL_PREFIX + self.name

    def harness_settings(self, harness: str) -> dict[str, Any]:
        return dict(self.harness.get(harness, {}))


def parse_profile(data: Any, *, brief_dir: Path, source: str) -> Profile:
    """Validate one decoded profile file.

    Raises:
        ProfileError: The file names something this daemon cannot honour.
    """

    def fail(message: str) -> ProfileError:
        return ProfileError(f"sys1 profile {source}: {message}")

    if not isinstance(data, dict):
        raise fail("the file must hold a JSON object")

    name = data.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise fail("'name' must be lowercase letters, digits and hyphens, 1 to 40 long")

    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise fail("'description' must be a non-empty string")

    tools = data.get("tools")
    if not isinstance(tools, list) or not tools or not all(isinstance(t, str) for t in tools):
        raise fail("'tools' must be a non-empty list of names")
    unknown = [tool for tool in tools if tool not in ABSTRACT_TOOLS]
    if unknown:
        raise fail(f"unknown tools {unknown}; allowed are {list(ABSTRACT_TOOLS)}")
    if len(set(tools)) != len(tools):
        raise fail("'tools' lists a tool twice")

    brief_name = data.get("brief")
    if not isinstance(brief_name, str) or not brief_name:
        raise fail("'brief' must name a template file")
    brief_path = (brief_dir / brief_name).resolve()
    if not brief_path.is_file():
        raise fail(f"the brief template {brief_path} does not exist")
    brief_template = brief_path.read_text(encoding="utf-8")

    workdir = data.get("workdir", "scratch")
    if not isinstance(workdir, str) or not (workdir == "scratch" or os.path.isabs(workdir)):
        raise fail("'workdir' must be 'scratch' or an absolute path")
    if any(tool in SHELL_TOOLS for tool in tools) and workdir != "scratch":
        raise fail("a profile that can write or run commands must use workdir 'scratch'")

    read_roots = data.get("read_roots")
    if read_roots is not None:
        if not isinstance(read_roots, list) or not all(
            isinstance(root, str) and os.path.isabs(root) for root in read_roots
        ):
            raise fail("'read_roots' must be null or a list of absolute paths")
        read_roots = tuple(os.path.normpath(root) for root in read_roots)

    timeout = data.get("timeout_seconds", 600)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise fail("'timeout_seconds' must be a positive number")

    harness = data.get("harness", {})
    if not isinstance(harness, dict) or not all(isinstance(v, dict) for v in harness.values()):
        raise fail("'harness' must map harness names to settings objects")
    pi_settings = harness.get("pi")
    if pi_settings is not None:
        for key in ("provider", "model"):
            if not isinstance(pi_settings.get(key), str) or not pi_settings[key]:
                raise fail(f"'harness.pi.{key}' must be a non-empty string")
        thinking = pi_settings.get("thinking", "high")
        if thinking not in THINKING_LEVELS:
            raise fail(f"'harness.pi.thinking' must be one of {list(THINKING_LEVELS)}")

    return Profile(
        name=name,
        description=description.strip(),
        tools=tuple(tools),
        brief_template=brief_template,
        workdir=workdir,
        read_roots=read_roots,
        timeout_seconds=float(timeout),
        harness={key: dict(value) for key, value in harness.items()},
    )


def load_profiles(directory: Path = BUILTIN_DIR) -> dict[str, Profile]:
    """Load every ``*.json`` profile in ``directory``, keyed by profile name."""
    profiles: dict[str, Profile] = {}
    if not directory.is_dir():
        raise ProfileError(f"sys1 profile directory {directory} does not exist")
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ProfileError(f"sys1 profile {path.name}: not valid JSON ({exc})") from exc
        profile = parse_profile(data, brief_dir=directory, source=path.name)
        if profile.name in profiles:
            raise ProfileError(f"sys1 profile {path.name}: the name {profile.name!r} is taken")
        profiles[profile.name] = profile
    if not profiles:
        raise ProfileError(f"sys1 profile directory {directory} holds no profile")
    return profiles


class ProfileCatalogue:
    """The profiles a daemon serves, and how model names resolve to them."""

    def __init__(
        self,
        profiles: dict[str, Profile],
        *,
        default: str,
        allow_shell: bool,
        accept_unknown: bool,
    ) -> None:
        self.profiles = profiles
        self.allow_shell = allow_shell
        self.accept_unknown = accept_unknown
        if default not in profiles:
            raise ProfileError(
                f"JEVMULATOR_SYS1_DEFAULT_PROFILE names {default!r}, which is not a profile; "
                f"the profiles are {sorted(profiles)}"
            )
        if profiles[default].is_shell and not allow_shell:
            raise ProfileError(
                f"the default sys1 profile {default!r} can write or run commands, so it "
                "needs JEVMULATOR_SYS1_ALLOW_SHELL=1"
            )
        self.default = profiles[default]

    def enabled(self) -> list[Profile]:
        return [p for p in self.profiles.values() if self.allow_shell or not p.is_shell]

    def resolve(self, model: str) -> Profile | None:
        """The profile a model name selects, or ``None`` when the name is not served.

        Raises:
            ProfileError: never. A disabled profile resolves to ``None``; the caller
                reports it, because only the caller knows how to phrase a 422.
        """
        if model in DEFAULT_ALIASES:
            return self.default
        if model.startswith(MODEL_PREFIX):
            profile = self.profiles.get(model[len(MODEL_PREFIX) :])
            if profile is not None and (self.allow_shell or not profile.is_shell):
                return profile
            return None
        if self.accept_unknown:
            return self.default
        return None

    def is_shell_gated(self, model: str) -> bool:
        """True when ``model`` names a real profile that only the shell flag hides."""
        if not model.startswith(MODEL_PREFIX):
            return False
        profile = self.profiles.get(model[len(MODEL_PREFIX) :])
        return profile is not None and profile.is_shell and not self.allow_shell

    def accepted_names(self) -> list[str]:
        return list(DEFAULT_ALIASES) + [profile.model_name for profile in self.enabled()]
