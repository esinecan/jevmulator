"""jevmulator.ps1 on this Windows host.

Every case runs the real scriptlet in a real PowerShell process against a real daemon.
The daemon uses the fake provider, so no network call leaves this machine.

Each test copies the repository's launch surface into its own temporary directory, so one
test's runtime file cannot reach another, and so a directory whose path contains a space
is covered.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

import pytest

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(sys.platform != "win32", reason="jevmulator.ps1 needs Windows"),
]

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_NAME = "jevmulator.ps1"
READY_TIMEOUT = "40"


@dataclass
class ScriptResult:
    code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + "\n" + self.stderr


class Workspace:
    """A throwaway copy of the launch surface, in a directory whose path has a space."""

    def __init__(self, root: str) -> None:
        self.root = root
        os.makedirs(root, exist_ok=True)
        shutil.copy2(os.path.join(REPO_ROOT, SCRIPT_NAME), os.path.join(root, SCRIPT_NAME))
        shutil.copytree(
            os.path.join(REPO_ROOT, "src"),
            os.path.join(root, "src"),
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        # The scriptlet dot-sources the identity helpers and refuses to run without them.
        shutil.copytree(
            os.path.join(REPO_ROOT, "lib"),
            os.path.join(root, "lib"),
            dirs_exist_ok=True,
        )

    @property
    def script(self) -> str:
        return os.path.join(self.root, SCRIPT_NAME)

    @property
    def runtime_file(self) -> str:
        return os.path.join(self.root, ".jevmulator", "runtime.json")

    def read_runtime(self) -> dict | None:
        if not os.path.exists(self.runtime_file):
            return None
        with open(self.runtime_file, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)

    def write_runtime(self, record: dict) -> None:
        os.makedirs(os.path.dirname(self.runtime_file), exist_ok=True)
        with open(self.runtime_file, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, indent=2)

    def run(self, *arguments: str, timeout: float = 120.0) -> ScriptResult:
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            self.script,
            *arguments,
        ]
        environment = dict(os.environ)
        environment["JEVMULATOR_PROVIDER"] = "fake"
        environment["JEVMULATOR_API_KEY"] = "lifecycle-test-key"
        environment["JEVMULATOR_PYTHON"] = sys.executable
        environment.pop("JEVMULATOR_PORT", None)
        environment.pop("JEVMULATOR_STATE_DIR", None)
        # Capture through files rather than pipes. The scriptlet launches a background
        # daemon, and on Windows a grandchild that inherits a pipe keeps it open after its
        # parent exits, so subprocess.run would block in communicate() even after the
        # timeout killed PowerShell itself.
        out_path = os.path.join(self.root, "_stdout.txt")
        err_path = os.path.join(self.root, "_stderr.txt")
        with open(out_path, "w", encoding="utf-8") as out_handle, open(
            err_path, "w", encoding="utf-8"
        ) as err_handle:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=out_handle,
                stderr=err_handle,
                timeout=timeout,
                cwd=self.root,
                env=environment,
            )
        with open(out_path, "r", encoding="utf-8", errors="replace") as handle:
            stdout = handle.read()
        with open(err_path, "r", encoding="utf-8", errors="replace") as handle:
            stderr = handle.read()
        return ScriptResult(completed.returncode, stdout, stderr)

    def stop_quietly(self) -> None:
        try:
            self.run("stop", timeout=60)
        except Exception:
            pass


@pytest.fixture
def workspace(tmp_path):
    """A workspace whose path contains a space, to catch quoting mistakes."""
    root = str(tmp_path / "jev workspace")
    space = Workspace(root)
    try:
        yield space
    finally:
        space.stop_quietly()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestStartStatusStop:
    def test_start_returns_only_after_the_daemon_is_ready(self, workspace) -> None:
        port = free_port()
        result = workspace.run("start", "-Port", str(port), "-ReadyTimeoutSeconds", READY_TIMEOUT)
        assert result.code == 0, result.output
        assert "ready on" in result.stdout

        # Ready means the health route already answers, with no extra wait here.
        import urllib.request

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/_jevmulator/health", timeout=5
        ) as response:
            assert json.loads(response.read().decode("utf-8"))["ready"] is True

    def test_the_runtime_file_records_the_port_and_process(self, workspace) -> None:
        port = free_port()
        workspace.run("start", "-Port", str(port), "-ReadyTimeoutSeconds", READY_TIMEOUT)
        record = workspace.read_runtime()
        assert record is not None
        assert record["port"] == port
        assert record["base_url"] == f"http://127.0.0.1:{port}"
        assert record["pid"] > 0
        assert record["process_start_time"]

    def test_status_reports_the_endpoint_and_readiness_without_a_port_argument(
        self, workspace
    ) -> None:
        port = free_port()
        workspace.run("start", "-Port", str(port), "-ReadyTimeoutSeconds", READY_TIMEOUT)
        result = workspace.run("status")
        assert result.code == 0, result.output
        assert f"http://127.0.0.1:{port}" in result.stdout
        assert "ready             : True" in result.stdout
        assert "upstream model    : glm-5.3-flash" in result.stdout

    def test_status_hides_the_key_unless_asked(self, workspace) -> None:
        port = free_port()
        workspace.run("start", "-Port", str(port), "-ReadyTimeoutSeconds", READY_TIMEOUT)
        hidden = workspace.run("status")
        assert "lifecycle-test-key" not in hidden.stdout
        assert "Add -ShowKey" in hidden.stdout
        shown = workspace.run("status", "-ShowKey")
        assert "lifecycle-test-key" in shown.stdout

    def test_status_before_any_start_says_not_running(self, workspace) -> None:
        result = workspace.run("status")
        assert result.code == 1
        assert "not running" in result.stdout

    def test_stop_terminates_the_daemon_and_removes_the_runtime_file(self, workspace) -> None:
        port = free_port()
        workspace.run("start", "-Port", str(port), "-ReadyTimeoutSeconds", READY_TIMEOUT)
        record = workspace.read_runtime()
        result = workspace.run("stop")
        assert result.code == 0, result.output
        assert "stopped" in result.stdout
        assert not os.path.exists(workspace.runtime_file)

        deadline = time.time() + 10
        while time.time() < deadline:
            if not _process_alive(record["pid"]):
                break
            time.sleep(0.2)
        assert not _process_alive(record["pid"])

    def test_stop_twice_is_safe(self, workspace) -> None:
        port = free_port()
        workspace.run("start", "-Port", str(port), "-ReadyTimeoutSeconds", READY_TIMEOUT)
        assert workspace.run("stop").code == 0
        second = workspace.run("stop")
        assert second.code == 0
        assert "not running" in second.stdout

    def test_stop_without_a_start_is_safe(self, workspace) -> None:
        result = workspace.run("stop")
        assert result.code == 0
        assert "not running" in result.stdout

    def test_start_stop_start_works(self, workspace) -> None:
        first_port = free_port()
        assert workspace.run(
            "start", "-Port", str(first_port), "-ReadyTimeoutSeconds", READY_TIMEOUT
        ).code == 0
        assert workspace.run("stop").code == 0
        second_port = free_port()
        assert workspace.run(
            "start", "-Port", str(second_port), "-ReadyTimeoutSeconds", READY_TIMEOUT
        ).code == 0
        assert workspace.read_runtime()["port"] == second_port


class TestDefaultPort:
    def test_start_without_a_port_uses_8769(self, workspace) -> None:
        if _port_in_use(8769):
            pytest.skip("port 8769 is already in use on this host")
        result = workspace.run("start", "-ReadyTimeoutSeconds", READY_TIMEOUT)
        assert result.code == 0, result.output
        assert workspace.read_runtime()["port"] == 8769
        assert "8769" in result.stdout

    def test_the_default_port_avoids_the_ports_this_host_already_uses(self) -> None:
        with open(os.path.join(REPO_ROOT, SCRIPT_NAME), "r", encoding="utf-8") as handle:
            script = handle.read()
        assert "$DefaultPort = 8769" in script
        for taken in ("8765", "8766", "8767", "8787", "8790", "8791"):
            assert f"$DefaultPort = {taken}" not in script


class TestDuplicateStart:
    def test_a_second_start_reports_the_running_daemon_and_does_not_launch_another(
        self, workspace
    ) -> None:
        port = free_port()
        workspace.run("start", "-Port", str(port), "-ReadyTimeoutSeconds", READY_TIMEOUT)
        first_pid = workspace.read_runtime()["pid"]
        second = workspace.run("start", "-Port", str(port), "-ReadyTimeoutSeconds", READY_TIMEOUT)
        assert second.code == 0
        assert "already running" in second.stdout
        assert workspace.read_runtime()["pid"] == first_pid


class TestPortCollision:
    """The documented exit codes must actually reach the caller.

    The scriptlet sets $ErrorActionPreference to Stop. Write-Error is therefore a
    terminating error, and it aborted the function before its `return <code>`, so every
    failure exited 1. Failure messages now go through Write-Failure, which writes to the
    error stream without terminating.
    """

    def test_an_occupied_port_is_reported_before_launching(self, workspace) -> None:
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        taken = blocker.getsockname()[1]
        try:
            result = workspace.run("start", "-Port", str(taken), "-ReadyTimeoutSeconds", "10")
            assert result.code == 2, result.output
            assert "already in use" in result.output
            assert not os.path.exists(workspace.runtime_file)
        finally:
            blocker.close()

    def test_the_daemon_is_never_launched_on_an_occupied_port(self, workspace) -> None:
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        taken = blocker.getsockname()[1]
        try:
            workspace.run("start", "-Port", str(taken), "-ReadyTimeoutSeconds", "10")
            out_log = os.path.join(workspace.root, ".jevmulator", "daemon.out.log")
            assert not os.path.exists(out_log) or os.path.getsize(out_log) == 0
        finally:
            blocker.close()

    def test_a_readiness_failure_returns_its_own_code(self, workspace) -> None:
        """Exit code 3 or 4, not the blanket 1 that a terminating Write-Error produced."""
        port = free_port()
        result = workspace.run(
            "start", "-Port", str(port), "-ReadyTimeoutSeconds", "20", "-Python", "not-a-python.exe"
        )
        assert result.code in (3, 4), result.output


class TestStaleRuntimeFile:
    def test_status_reports_not_running_for_a_dead_process(self, workspace) -> None:
        workspace.write_runtime(
            {
                "pid": _dead_pid(),
                "port": 8769,
                "base_url": "http://127.0.0.1:8769",
                "health_url": "http://127.0.0.1:8769/_jevmulator/health",
                "api_key": "stale",
                "process_start_time": "2020-01-01T00:00:00.0000000+00:00",
            }
        )
        result = workspace.run("status")
        assert result.code == 1
        assert "not running" in result.stdout

    def test_stop_removes_a_stale_runtime_file(self, workspace) -> None:
        workspace.write_runtime(
            {
                "pid": _dead_pid(),
                "port": 8769,
                "base_url": "http://127.0.0.1:8769",
                "health_url": "http://127.0.0.1:8769/_jevmulator/health",
                "api_key": "stale",
                "process_start_time": "2020-01-01T00:00:00.0000000+00:00",
            }
        )
        result = workspace.run("stop")
        assert result.code == 0
        assert "stale runtime file" in result.stdout
        assert not os.path.exists(workspace.runtime_file)

    def test_start_replaces_a_stale_runtime_file(self, workspace) -> None:
        port = free_port()
        workspace.write_runtime(
            {
                "pid": _dead_pid(),
                "port": port,
                "base_url": f"http://127.0.0.1:{port}",
                "health_url": f"http://127.0.0.1:{port}/_jevmulator/health",
                "api_key": "stale",
                "process_start_time": "2020-01-01T00:00:00.0000000+00:00",
            }
        )
        result = workspace.run("start", "-Port", str(port), "-ReadyTimeoutSeconds", READY_TIMEOUT)
        assert result.code == 0, result.output
        assert "stale runtime file" in result.stdout
        assert workspace.read_runtime()["pid"] != _dead_pid()

    def test_an_unreadable_runtime_file_does_not_stop_a_start(self, workspace) -> None:
        os.makedirs(os.path.dirname(workspace.runtime_file), exist_ok=True)
        with open(workspace.runtime_file, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        port = free_port()
        result = workspace.run("start", "-Port", str(port), "-ReadyTimeoutSeconds", READY_TIMEOUT)
        assert result.code == 0, result.output


class TestUnrelatedProcessProtection:
    def test_stop_never_terminates_a_process_that_is_not_the_daemon(self, workspace) -> None:
        """A recycled process id must not be killed.

        The runtime file names a live process this test owns, with a start time that does
        not match. Stop must refuse, and the process must still be alive afterwards.
        """
        victim = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            workspace.write_runtime(
                {
                    "pid": victim.pid,
                    "port": 8769,
                    "base_url": "http://127.0.0.1:8769",
                    "health_url": "http://127.0.0.1:8769/_jevmulator/health",
                    "api_key": "not-ours",
                    "process_start_time": "2020-01-01T00:00:00.0000000+00:00",
                }
            )
            result = workspace.run("stop")
            assert result.code == 0, result.output
            time.sleep(0.5)
            assert victim.poll() is None, "stop terminated an unrelated process"
        finally:
            victim.kill()
            victim.wait(timeout=10)

    def test_status_does_not_claim_an_unrelated_process_is_the_daemon(self, workspace) -> None:
        victim = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            workspace.write_runtime(
                {
                    "pid": victim.pid,
                    "port": 8769,
                    "base_url": "http://127.0.0.1:8769",
                    "health_url": "http://127.0.0.1:8769/_jevmulator/health",
                    "api_key": "not-ours",
                    "process_start_time": "2020-01-01T00:00:00.0000000+00:00",
                }
            )
            result = workspace.run("status")
            assert result.code == 1
            assert "not running" in result.stdout
            assert "Identity check refused" in result.stdout
            assert "is a different process" in result.stdout
        finally:
            victim.kill()
            victim.wait(timeout=10)


class TestPathsWithSpaces:
    def test_the_workspace_path_contains_a_space(self, workspace) -> None:
        assert " " in workspace.root

    def test_everything_works_from_a_path_with_a_space(self, workspace) -> None:
        port = free_port()
        assert workspace.run(
            "start", "-Port", str(port), "-ReadyTimeoutSeconds", READY_TIMEOUT
        ).code == 0
        assert workspace.run("status").code == 0
        assert workspace.run("stop").code == 0

    def test_the_log_files_land_inside_the_workspace(self, workspace) -> None:
        port = free_port()
        workspace.run("start", "-Port", str(port), "-ReadyTimeoutSeconds", READY_TIMEOUT)
        out_log = os.path.join(workspace.root, ".jevmulator", "daemon.out.log")
        assert os.path.exists(out_log)
        with open(out_log, "r", encoding="utf-8") as handle:
            assert "listening on" in handle.read()


class TestRestart:
    def test_restart_stops_then_starts(self, workspace) -> None:
        port = free_port()
        workspace.run("start", "-Port", str(port), "-ReadyTimeoutSeconds", READY_TIMEOUT)
        first_pid = workspace.read_runtime()["pid"]
        result = workspace.run("restart", "-Port", str(port), "-ReadyTimeoutSeconds", READY_TIMEOUT)
        assert result.code == 0, result.output
        assert workspace.read_runtime()["pid"] != first_pid


class TestArgumentValidation:
    def test_an_unknown_command_is_rejected(self, workspace) -> None:
        result = workspace.run("explode")
        assert result.code != 0
        assert "explode" in result.output

    def test_an_out_of_range_port_is_rejected(self, workspace) -> None:
        result = workspace.run("start", "-Port", "70000")
        assert result.code != 0


# -- helpers --------------------------------------------------------------


def _process_alive(pid: int) -> bool:
    """True when that process id still exists. Uses the Win32 API, not a new shell."""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def _dead_pid() -> int:
    """A process id that has certainly exited."""
    process = subprocess.Popen(
        [sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    process.wait(timeout=30)
    return process.pid


class TestIdentityCheckFailsClosed:
    """Get-DaemonProcess must refuse to act whenever a proof is unavailable.

    The earlier version failed open in three ways. It accepted a runtime file with no
    recorded creation time, it accepted an unreadable command line, and it accepted any
    command line containing the word jevmulator, which also matches a daemon belonging to
    a different checkout. Each of those would have let `stop` terminate something this
    checkout does not own.

    No case here terminates an unrelated process. The one live process a case creates is
    owned by that case and is killed by the case itself, not by the scriptlet.
    """

    def _runtime(self, pid: int, port: int, **overrides) -> dict:
        record = {
            "pid": pid,
            "port": port,
            "base_url": f"http://127.0.0.1:{port}",
            "health_url": f"http://127.0.0.1:{port}/_jevmulator/health",
            "api_key": "lifecycle-test-key",
        }
        record.update(overrides)
        return record

    def test_a_missing_creation_time_is_refused(self, workspace) -> None:
        victim = _sleeping_process()
        try:
            workspace.write_runtime(self._runtime(victim.pid, 8769))
            result = workspace.run("stop")
            assert result.code == 0, result.output
            assert "Identity check refused" in result.stdout
            assert "records no creation time" in result.stdout
            time.sleep(0.4)
            assert victim.poll() is None, "stop terminated a process it could not verify"
        finally:
            _kill(victim)

    def test_a_malformed_creation_time_is_refused(self, workspace) -> None:
        victim = _sleeping_process()
        try:
            workspace.write_runtime(
                self._runtime(victim.pid, 8769, process_start_time="not a date at all")
            )
            result = workspace.run("stop")
            assert result.code == 0, result.output
            assert "is not a date" in result.stdout
            time.sleep(0.4)
            assert victim.poll() is None
        finally:
            _kill(victim)

    def test_a_mismatched_creation_time_is_refused(self, workspace) -> None:
        victim = _sleeping_process()
        try:
            workspace.write_runtime(
                self._runtime(
                    victim.pid,
                    8769,
                    process_start_time="2020-01-01T00:00:00.0000000+00:00",
                )
            )
            result = workspace.run("stop")
            assert result.code == 0, result.output
            assert "is a different process" in result.stdout
            time.sleep(0.4)
            assert victim.poll() is None
        finally:
            _kill(victim)

    def test_a_process_that_is_not_a_daemon_is_refused(self, workspace) -> None:
        """Correct creation time, but the command line does not invoke the daemon."""
        victim = _sleeping_process()
        try:
            workspace.write_runtime(
                self._runtime(victim.pid, 8769, process_start_time=_start_time_of(victim.pid))
            )
            result = workspace.run("stop")
            assert result.code == 0, result.output
            assert "is not this checkout's daemon" in result.stdout
            assert "does not invoke -m jevmulator serve" in result.stdout
            time.sleep(0.4)
            assert victim.poll() is None
        finally:
            _kill(victim)

    def test_a_daemon_of_a_different_checkout_is_refused(self, workspace, tmp_path) -> None:
        """A real Jevmulator daemon, but one that owns another state directory.

        The word jevmulator appears in its command line, which the earlier check accepted.
        The state-directory proof rejects it.
        """
        other = Workspace(str(tmp_path / "other checkout"))
        port = free_port()
        try:
            started = other.run("start", "-Port", str(port), "-ReadyTimeoutSeconds", READY_TIMEOUT)
            assert started.code == 0, started.output
            other_record = other.read_runtime()

            # This workspace claims the other checkout's daemon, with a correct pid and a
            # correct creation time. Only the state directory differs.
            workspace.write_runtime(
                self._runtime(
                    other_record["pid"],
                    other_record["port"],
                    process_start_time=other_record["process_start_time"],
                )
            )
            result = workspace.run("stop")
            assert result.code == 0, result.output
            assert "is not this checkout's daemon" in result.stdout
            # The refusal names the directory the process really owns, which is the other
            # workspace, not this one.
            assert "it owns" in result.stdout
            assert other.root in result.stdout

            # The other daemon is untouched and still answers.
            assert _process_alive(other_record["pid"])
            assert other.run("status").code == 0
        finally:
            other.stop_quietly()

    def test_status_states_the_refusal_reason(self, workspace) -> None:
        victim = _sleeping_process()
        try:
            workspace.write_runtime(self._runtime(victim.pid, 8769))
            result = workspace.run("status")
            assert result.code == 1
            assert "Identity check refused" in result.stdout
            time.sleep(0.4)
            assert victim.poll() is None
        finally:
            _kill(victim)

    def test_a_verified_daemon_is_still_stopped(self, workspace) -> None:
        """The check refuses what it cannot prove, and still acts on what it can."""
        port = free_port()
        assert workspace.run(
            "start", "-Port", str(port), "-ReadyTimeoutSeconds", READY_TIMEOUT
        ).code == 0
        record = workspace.read_runtime()
        result = workspace.run("stop")
        assert result.code == 0, result.output
        assert "stopped" in result.stdout
        deadline = time.time() + 10
        while time.time() < deadline and _process_alive(record["pid"]):
            time.sleep(0.2)
        assert not _process_alive(record["pid"])


def _sleeping_process() -> subprocess.Popen:
    """A live process this test owns, which the scriptlet must never terminate."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _kill(process: subprocess.Popen) -> None:
    process.kill()
    process.wait(timeout=10)


def _start_time_of(pid: int) -> str:
    """The creation time of a process, in the round-trip format the scriptlet writes."""
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"(Get-Process -Id {pid}).StartTime.ToString('o',"
            " [System.Globalization.CultureInfo]::InvariantCulture)",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return completed.stdout.strip()
