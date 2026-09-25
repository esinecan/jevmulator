"""Harness adapters: the programs that run a sys1 agent.

The rules of a run live in the daemon, behind two HTTP endpoints (``hello`` and the form).
An adapter only has to start its agent with the brief and the granted tools, expose a
``submit_verdict`` tool that posts to the form, report the tools and model it actually got,
and write its events as JSON lines. Adding a harness means adding one adapter.
"""

from __future__ import annotations

from ...config import Config
from .base import Harness, HarnessProcess, LaunchSpec


def build_harness(config: Config) -> Harness:
    if config.sys1_harness == "fake":
        from .fake import FakeHarness

        return FakeHarness(config)
    from .pi import PiHarness

    return PiHarness(config)


__all__ = ["Harness", "HarnessProcess", "LaunchSpec", "build_harness"]
