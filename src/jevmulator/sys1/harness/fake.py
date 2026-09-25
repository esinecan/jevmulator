"""The fake harness: a scripted Python agent, for tests and hand tests.

It is a real child process in a real job, and it speaks the real protocol: ``hello``,
pi-shaped JSON events on stdout, and the form. Only the judgment is scripted. Its script
comes from ``JEVMULATOR_SYS1_FAKE_SCRIPT``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ...config import Config
from ..jobs import RunJob
from ..profiles import Profile
from .base import Harness, HarnessProcess, LaunchSpec, child_environment, run_variables

AGENT_SCRIPT = Path(__file__).resolve().parent.parent / "fake_agent.py"
FAKE_MODEL = "fake/fake-agent"


class FakeHarness(Harness):
    name = "fake"

    def __init__(self, config: Config) -> None:
        self._config = config

    def problems(self) -> list[str]:
        script = self._config.sys1_fake_script
        if script and not os.path.isfile(script):
            return [f"JEVMULATOR_SYS1_FAKE_SCRIPT names {script}, which does not exist."]
        return []

    def model_id(self, profile: Profile) -> str:
        return FAKE_MODEL

    def tool_names(self, profile: Profile) -> list[str]:
        return sorted(set(profile.tools) | {"submit_verdict"})

    def launch(self, spec: LaunchSpec) -> HarnessProcess:
        extra = run_variables(spec, replace_bash="shell" in spec.tools)
        extra.update(
            {
                "SYS1_RUN_DIR": str(spec.run_dir),
                "SYS1_FAKE_SCRIPT": self._config.sys1_fake_script,
                "SYS1_EXPECTED_TOOLS": ",".join(sorted(set(spec.tools) | {"submit_verdict"})),
                "SYS1_EXPECTED_MODEL": FAKE_MODEL,
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        job = RunJob()
        with open(spec.events_path, "ab") as out, open(spec.stderr_path, "ab") as err:
            popen = job.spawn(
                [sys.executable, str(AGENT_SCRIPT)],
                cwd=str(spec.work_dir),
                env=child_environment(extra),
                stdout=out,
                stderr=err,
            )
        return HarnessProcess(job, popen, spec)
