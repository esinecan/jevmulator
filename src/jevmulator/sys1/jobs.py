"""Process containment for one sys1 run.

On Windows every process of a run lives in one job object with ``KILL_ON_JOB_CLOSE``. The
root process starts suspended, joins the job, and only then resumes, so everything it
starts is inside the job from its first instruction. When the job's last handle closes,
Windows ends every process in it, however this daemon ends. That matters because
``jevmulator.ps1 stop`` ends a daemon that does not exit within 10 seconds with
``Stop-Process -Force``, and a forced stop runs no Python cleanup at all.

The Windows part is ported from the ``Job`` class in Eren's
``agent-personal-space/experiments/tui-mcp-eval-2026-09-22/bundle/core.py``, without its
``psutil`` dependency. See NOTICES.md.

On POSIX a run's root starts a new session and is killed as a process group.
"""

from __future__ import annotations

import ctypes
import logging
import os
import signal
import subprocess
import time
from typing import Any

LOGGER = logging.getLogger("jevmulator.sys1")

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

#: Windows FILETIME counts 100 ns intervals since 1601-01-01.
_FILETIME_EPOCH_OFFSET = 116444736000000000
_MAX_LISTED_PIDS = 1024

IS_WINDOWS = os.name == "nt"


if IS_WINDOWS:
    from ctypes import wintypes

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _Accounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class _PidList(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * _MAX_LISTED_PIDS),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long


def process_creation_time(pid: int) -> float | None:
    """Creation time of ``pid`` in seconds since the epoch, or ``None`` if unreadable."""
    if not IS_WINDOWS:
        return None
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not _kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        ):
            return None
        value = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return (value - _FILETIME_EPOCH_OFFSET) / 10_000_000
    finally:
        _kernel32.CloseHandle(handle)


class RunJob:
    """Every process of one run. Terminating it ends every one of them."""

    def __init__(self) -> None:
        self._handle = None
        self._popen: subprocess.Popen | None = None
        self._closed = False
        self._observed: dict[int, float | None] = {}
        self.containment = "job-object" if IS_WINDOWS else "process-group"
        if IS_WINDOWS:
            handle = _kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _ExtendedLimit()
            limits.BasicLimitInformation.LimitFlags = (
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
            )
            if not _kernel32.SetInformationJobObject(
                handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits), ctypes.sizeof(limits),
            ):
                error = ctypes.WinError(ctypes.get_last_error())
                _kernel32.CloseHandle(handle)
                raise error
            self._handle = handle

    # -- launch ------------------------------------------------------------

    def spawn(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        stdout: Any,
        stderr: Any,
    ) -> subprocess.Popen:
        """Start the run's root process inside the job. Only one root per run."""
        if self._popen is not None:
            raise RuntimeError("a run job holds one root process")
        if not IS_WINDOWS:
            self._popen = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, start_new_session=True,
            )
            self._observed[self._popen.pid] = None
            return self._popen

        popen = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            creationflags=CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        )
        process_handle = int(popen._handle)  # noqa: SLF001 - the only way to reach the handle
        if not _kernel32.AssignProcessToJobObject(self._handle, process_handle):
            error = ctypes.WinError(ctypes.get_last_error())
            popen.kill()
            popen.wait(timeout=5)
            raise error
        status = _ntdll.NtResumeProcess(process_handle)
        if status != 0:
            popen.kill()
            popen.wait(timeout=5)
            raise OSError(f"NtResumeProcess failed with status {status:#x}")
        self._popen = popen
        self.observe()
        return popen

    # -- observation ---------------------------------------------------------

    def pids(self) -> list[int]:
        """Process ids currently in the job."""
        if not IS_WINDOWS:
            if self._popen is not None and self._popen.poll() is None:
                return [self._popen.pid]
            return []
        if self._handle is None:
            return []
        listing = _PidList()
        if not _kernel32.QueryInformationJobObject(
            self._handle, JOB_OBJECT_BASIC_PROCESS_ID_LIST,
            ctypes.byref(listing), ctypes.sizeof(listing), None,
        ):
            return []
        count = min(listing.NumberOfProcessIdsInList, _MAX_LISTED_PIDS)
        return [int(listing.ProcessIdList[index]) for index in range(count)]

    def observe(self) -> None:
        """Remember every process now in the job, with its creation time."""
        for pid in self.pids():
            if pid not in self._observed:
                self._observed[pid] = process_creation_time(pid)

    def observed(self) -> list[dict[str, Any]]:
        return [
            {"pid": pid, "created_at": created}
            for pid, created in sorted(self._observed.items())
        ]

    def active_processes(self) -> int:
        if not IS_WINDOWS:
            if self._popen is None:
                return 0
            try:
                os.killpg(self._popen.pid, 0)
            except (ProcessLookupError, PermissionError):
                return 0
            return 1
        if self._handle is None:
            return 0
        info = _Accounting()
        if not _kernel32.QueryInformationJobObject(
            self._handle, JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info), None,
        ):
            return -1
        return int(info.ActiveProcesses)

    # -- ending ----------------------------------------------------------------

    def terminate(self, *, wait_seconds: float = 5.0) -> int:
        """End every process of the run, then report how many are still active.

        The job handle stays open until :meth:`close`, so the count can be read.
        """
        if self._popen is None:
            return 0
        self.observe()
        if IS_WINDOWS:
            if self._handle is not None and not _kernel32.TerminateJobObject(self._handle, 1):
                LOGGER.warning(
                    "TerminateJobObject failed (%s); falling back to taskkill",
                    ctypes.get_last_error(),
                )
                subprocess.run(
                    ["taskkill", "/PID", str(self._popen.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, check=False,
                )
        else:
            try:
                os.killpg(self._popen.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + wait_seconds
        while True:
            remaining = self.active_processes()
            if remaining == 0 or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        try:
            self._popen.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        return remaining

    def close(self) -> None:
        """Release the job. On Windows this also ends anything still inside it."""
        if self._closed:
            return
        self._closed = True
        if IS_WINDOWS and self._handle is not None:
            _kernel32.CloseHandle(self._handle)
            self._handle = None
