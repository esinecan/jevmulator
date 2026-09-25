"""The pi adapter.

pi runs through ``node`` on its own JavaScript entry point, never through the ``pi.cmd``
shim, so no argument passes through ``cmd.exe``. The argument list is fixed and holds no
caller text. Isolation flags keep the user's pi packages, MCP servers, skills and context
files out of the run, and a private agent directory keeps the run out of the user's pi
session store. The judge extension ``pi_judge.ts`` adds ``submit_verdict``, reports the
tools and model pi actually offered, and confines the file and shell tools.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from ...config import Config
from ..jobs import RunJob
from ..profiles import Profile
from .base import Harness, HarnessProcess, LaunchSpec, child_environment, run_variables

EXTENSION = Path(__file__).resolve().with_name("pi_judge.ts")

#: Abstract tool names to pi tool names. pi's ``powershell`` tool is never granted: it is
#: Windows PowerShell 5.1, and its ``>`` writes UTF-16LE.
PI_TOOLS = {
    "read": "read",
    "grep": "grep",
    "find": "find",
    "ls": "ls",
    "shell": "bash",
    "write": "write",
    "edit": "edit",
}

#: The first user message. It is a constant, so no caller text reaches the command line.
KICKOFF = "Judge the questions in your brief. Finish by calling submit_verdict."

#: The private agent directory's settings. ``packages`` is empty, so none of the user's
#: pi packages load even if ``--no-extensions`` were ever dropped.
PI_SETTINGS = {
    "defaultProvider": "zai",
    "defaultModel": "glm-5.3-flash",
    "packages": [],
    "quietStartup": True,
    "retry": {"maxRetries": 2},
    "compaction": {"enabled": False},
}

#: The search tools pi calls. With ``--offline`` pi does not download them.
SEARCH_BINARIES = ("rg.exe", "fd.exe") if os.name == "nt" else ("rg", "fd")


def derive_cli() -> str:
    """pi's entry point, found next to the ``pi`` shim on PATH."""
    shim = shutil.which("pi")
    if not shim:
        return ""
    candidate = (
        Path(shim).resolve().parent
        / "node_modules"
        / "@earendil-works"
        / "pi-coding-agent"
        / "dist"
        / "bundle"
        / "cli.js"
    )
    return str(candidate) if candidate.is_file() else ""


class PiHarness(Harness):
    name = "pi"

    def __init__(self, config: Config) -> None:
        self._config = config
        self.node = config.sys1_node or shutil.which("node") or ""
        self.cli = config.sys1_pi_cli or derive_cli()
        self.agent_dir = Path(config.sys1_home) / "pi-agent"
        self._problems = self._check()

    # -- readiness -----------------------------------------------------------

    def _check(self) -> list[str]:
        problems: list[str] = []
        if not self.node or not os.path.isfile(self.node):
            problems.append("node was not found. Set JEVMULATOR_SYS1_NODE to node.exe.")
        if not self.cli or not os.path.isfile(self.cli):
            problems.append(
                "pi's entry point cli.js was not found. Set JEVMULATOR_SYS1_PI_CLI."
            )
        if not EXTENSION.is_file():
            problems.append(f"The judge extension {EXTENSION} is missing.")
        if not os.environ.get("ZAI_API_KEY"):
            problems.append("ZAI_API_KEY is not set. pi's zai provider reads it.")
        try:
            problems.extend(self._prepare_agent_dir())
        except OSError as exc:
            problems.append(f"The private pi agent directory could not be prepared: {exc}")
        return problems

    def _prepare_agent_dir(self) -> list[str]:
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        settings_path = self.agent_dir / "settings.json"
        wanted = json.dumps(PI_SETTINGS, indent=2) + "\n"
        if not settings_path.exists() or settings_path.read_text(encoding="utf-8") != wanted:
            settings_path.write_text(wanted, encoding="utf-8", newline="\n")
        bin_dir = self.agent_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        source = Path.home() / ".pi" / "agent" / "bin"
        missing = []
        for name in SEARCH_BINARIES:
            target = bin_dir / name
            if not target.is_file() and (source / name).is_file():
                shutil.copy2(source / name, target)
            if not target.is_file():
                missing.append(name)
        if missing:
            return [
                f"pi's search tools {missing} are missing from {bin_dir}; copy them from "
                "~/.pi/agent/bin."
            ]
        return []

    def problems(self) -> list[str]:
        return list(self._problems)

    # -- identity ----------------------------------------------------------------

    def model_id(self, profile: Profile) -> str:
        settings = profile.harness_settings("pi")
        return f"{settings['provider']}/{settings['model']}"

    def tool_names(self, profile: Profile) -> list[str]:
        return sorted({PI_TOOLS[tool] for tool in profile.tools} | {"submit_verdict"})

    # -- launch --------------------------------------------------------------------

    def command(self, spec: LaunchSpec) -> list[str]:
        settings = spec.settings
        tools = [PI_TOOLS[tool] for tool in spec.tools] + ["submit_verdict"]
        return [
            self.node,
            self.cli,
            "--mode", "json",
            "-p",
            "--offline",
            "--no-approve",
            "--provider", settings["provider"],
            "--model", settings["model"],
            "--thinking", settings.get("thinking", "high"),
            "--system-prompt", str(spec.brief_path),
            "--no-extensions",
            "-e", str(EXTENSION),
            "--no-skills",
            "--no-context-files",
            "--no-prompt-templates",
            "--no-themes",
            "--tools", ",".join(tools),
            "--session-dir", str(spec.session_dir),
            KICKOFF,
        ]

    def environment(self, spec: LaunchSpec) -> dict[str, str]:
        extra = run_variables(spec, replace_bash="shell" in spec.tools)
        extra["SYS1_RUN_DIR"] = str(spec.run_dir)
        extra["PI_CODING_AGENT_DIR"] = str(self.agent_dir)
        return child_environment(extra, passthrough=("ZAI_API_KEY",))

    def launch(self, spec: LaunchSpec) -> HarnessProcess:
        # pi reads a --system-prompt path as a file only when the file exists. Otherwise it
        # uses the path string itself as the whole prompt.
        if not spec.brief_path.is_file():
            raise OSError(f"the brief {spec.brief_path} does not exist")
        job = RunJob()
        with open(spec.events_path, "ab") as out, open(spec.stderr_path, "ab") as err:
            popen = job.spawn(
                self.command(spec),
                cwd=str(spec.work_dir),
                env=self.environment(spec),
                stdout=out,
                stderr=err,
            )
        return HarnessProcess(job, popen, spec)
