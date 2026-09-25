"""The pinned official JavaScript SDK, driven over real HTTP against this daemon.

Pin: ``@typesafe-ai/sdk@0.6.0``, contract commit ``66880ccded6cb642dc1809620c2b108c33730214``.

The cases live in ``tests/js/run.mjs`` and run under Node. This module starts a real
daemon, runs that script against it, and reports each case as part of the Python suite.

Install the JavaScript dependency once with::

    cd tests/js && npm ci
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from conftest import start_daemon

pytestmark = pytest.mark.js

JS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js")
NODE_MODULES = os.path.join(JS_DIR, "node_modules", "@typesafe-ai", "sdk")


def _node() -> str | None:
    return shutil.which("node")


requires_node = pytest.mark.skipif(_node() is None, reason="node is not installed")
requires_sdk = pytest.mark.skipif(
    not os.path.isdir(NODE_MODULES),
    reason="@typesafe-ai/sdk@0.6.0 is not installed; run: cd tests/js && npm ci",
)


@pytest.fixture(scope="module")
def js_result():
    """Run the whole JavaScript suite once against one daemon."""
    if _node() is None or not os.path.isdir(NODE_MODULES):
        pytest.skip("node or @typesafe-ai/sdk is missing")

    with start_daemon(JEVMULATOR_PROVIDER="fake") as running:
        environment = dict(os.environ)
        environment["JEVMULATOR_BASE_URL"] = running.client.base_url
        environment["JEVMULATOR_API_KEY"] = running.client.api_key
        completed = subprocess.run(
            [_node(), "run.mjs"],
            cwd=JS_DIR,
            capture_output=True,
            text=True,
            timeout=300,
            env=environment,
        )

    summary_line = ""
    for line in reversed(completed.stdout.strip().splitlines()):
        if line.startswith("{"):
            summary_line = line
            break
    summary = json.loads(summary_line) if summary_line else {}
    return completed, summary


@requires_node
@requires_sdk
class TestJavaScriptSdk:
    def test_the_installed_sdk_is_the_pinned_version(self) -> None:
        with open(
            os.path.join(NODE_MODULES, "package.json"), "r", encoding="utf-8"
        ) as handle:
            package = json.load(handle)
        assert package["name"] == "@typesafe-ai/sdk"
        assert package["version"] == "0.6.0"

    def test_the_pin_is_exact_in_the_manifest(self) -> None:
        with open(os.path.join(JS_DIR, "package.json"), "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        assert manifest["dependencies"]["@typesafe-ai/sdk"] == "0.6.0"

    def test_every_javascript_case_passes(self, js_result) -> None:
        completed, summary = js_result
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert summary.get("failed") == 0, summary
        assert summary.get("passed", 0) >= 14

    def test_the_summary_names_the_pinned_sdk(self, js_result) -> None:
        _completed, summary = js_result
        assert summary.get("sdk") == "0.6.0"

    def test_the_case_names_are_reported(self, js_result) -> None:
        completed, _summary = js_result
        assert "a mixed batch round trips" in completed.stdout
        assert "model discovery lists the catalogue" in completed.stdout
        assert "structured instructions and criteria survive" in completed.stdout
        assert "sys1 answers through a base URL with a path prefix" in completed.stdout
        assert "sys1 model discovery lists the profiles" in completed.stdout
