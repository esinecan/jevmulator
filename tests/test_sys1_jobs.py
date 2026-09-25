"""Process containment: every process of a run ends with the run, however the daemon ends.

``jevmulator.ps1 stop`` ends a daemon that does not exit within 10 seconds with
``Stop-Process -Force``, which runs no Python cleanup. Only the job object's
KILL_ON_JOB_CLOSE then ends the agent and its children. The last test proves that with a
real daemon process killed by TerminateProcess.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from jevmulator.sys1.jobs import RunJob
from test_sys1_http import MIXED, pid_alive, wait_for

REPO_ROOT = Path(__file__).resolve().parents[1]

windows_only = pytest.mark.skipif(os.name != "nt", reason="job objects exist only on Windows")


def spawn_with_grandchild(job: RunJob, workdir: Path) -> tuple[subprocess.Popen, int]:
    pid_file = workdir / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        f"open(r'{pid_file}', 'w').write(str(child.pid))\n"
        "time.sleep(600)\n"
    )
    with open(workdir / "out.log", "wb") as out:
        popen = job.spawn(
            [sys.executable, "-c", code], cwd=str(workdir), env=dict(os.environ),
            stdout=out, stderr=subprocess.STDOUT,
        )
    grandchild = wait_for(
        lambda: pid_file.is_file() and pid_file.read_text().strip() and int(pid_file.read_text()),
        what="the grandchild",
    )
    return popen, grandchild


@windows_only
def test_terminate_ends_the_root_and_the_grandchild(tmp_path):
    job = RunJob()
    popen, grandchild = spawn_with_grandchild(job, tmp_path)
    job.observe()
    assert popen.pid in job.pids() and grandchild in job.pids()
    assert job.terminate() == 0
    job.close()
    assert not pid_alive(popen.pid)
    assert not pid_alive(grandchild)
    recorded = {entry["pid"] for entry in job.observed()}
    assert {popen.pid, grandchild} <= recorded


@windows_only
def test_closing_the_last_handle_ends_the_job(tmp_path):
    job = RunJob()
    popen, grandchild = spawn_with_grandchild(job, tmp_path)
    job.close()
    wait_for(lambda: not pid_alive(grandchild) and not pid_alive(popen.pid), what="the job to end")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@windows_only
def test_a_hard_killed_daemon_leaves_no_agent_behind(tmp_path):
    port = free_port()
    home = tmp_path / "home"
    script = tmp_path / "script.json"
    script.write_text(
        json.dumps({"steps": [{"do": "grandchild", "seconds": 600}, {"do": "busy", "seconds": 120}]}),
        encoding="utf-8",
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("JEVMULATOR_")}
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "JEVMULATOR_API_KEY": "k",
            "JEVMULATOR_PROVIDER": "fake",
            "JEVMULATOR_SYS1_HARNESS": "fake",
            "JEVMULATOR_SYS1_HOME": str(home),
            "JEVMULATOR_SYS1_FAKE_SCRIPT": str(script),
        }
    )
    # Output goes to files, never to pipes: a grandchild that inherits a pipe would keep a
    # reader waiting for end-of-file forever.
    with open(tmp_path / "daemon.out", "wb") as out, open(tmp_path / "daemon.err", "wb") as err:
        daemon = subprocess.Popen(
            [sys.executable, "-m", "jevmulator", "serve", "--port", str(port), "--state-dir", str(tmp_path / "state")],
            env=env, cwd=tmp_path, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            creationflags=0x08000000,
        )
    try:
        wait_for(lambda: _healthy(port), timeout=30, what="the daemon to be ready")

        def call():
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/sys1/v1/systemone",
                data=json.dumps(MIXED).encode("utf-8"),
                headers={"Authorization": "Bearer k", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(request, timeout=120).read()
            except Exception:  # noqa: BLE001 - the daemon is killed under this request
                pass

        threading.Thread(target=call, daemon=True).start()

        def grandchild():
            for path in (home / "runs").glob("*/work/grandchild.pid"):
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return int(text)
            return None

        grandchild_pid = wait_for(grandchild, what="the agent's grandchild")
        record = next((home / "runs").glob("*/record.json"))
        agent_pids = {entry["pid"] for entry in json.loads(record.read_text(encoding="utf-8"))["processes"]}
        assert pid_alive(grandchild_pid)

        daemon.kill()  # TerminateProcess: no Python cleanup runs.
        daemon.wait(timeout=30)

        wait_for(lambda: not pid_alive(grandchild_pid), what="the grandchild to end with the daemon")
        for pid in agent_pids:
            wait_for(lambda pid=pid: not pid_alive(pid), what=f"agent process {pid} to end")
    finally:
        if daemon.poll() is None:
            daemon.kill()


def _healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/_jevmulator/health", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False
