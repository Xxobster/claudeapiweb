"""Spawn a child and kill its whole process tree (docs/THREATS.md T-22).

Windows: the child is assigned to a Job Object with KILL_ON_JOB_CLOSE, so the tree dies on
`kill()` and also if CPM itself dies. POSIX (CI, development): the child leads a new session and
the tree is killed by process group. Arguments are always a list (T-04); the environment is passed
in explicitly by the caller (T-12).
"""

from __future__ import annotations

import os
import signal
import subprocess  # noqa: S404 (argument lists only, see tests/security/test_injection.py)
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


class _WinJob:
    """Minimal ctypes Job Object. Only constructed on Windows."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class Io(ctypes.Structure):
            _fields_ = [
                (n, ctypes.c_ulonglong)
                for n in (
                    "ReadOps",
                    "WriteOps",
                    "OtherOps",
                    "ReadBytes",
                    "WriteBytes",
                    "OtherBytes",
                )
            ]

        class Ext(ctypes.Structure):
            _fields_ = [
                ("Basic", Basic),
                ("Io", Io),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k32 = k32
        self._job = k32.CreateJobObjectW(None, None)
        if not self._job:
            raise OSError("CreateJobObjectW failed")
        info = Ext()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(
            self._job, 9, ctypes.byref(info), ctypes.sizeof(info)
        )  # JobObjectExtendedLimitInformation
        if not ok:
            raise OSError("SetInformationJobObject failed")

    def assign(self, popen: subprocess.Popen[bytes]) -> None:
        if not self._k32.AssignProcessToJobObject(self._job, int(popen._handle)):  # type: ignore[attr-defined]
            raise OSError("AssignProcessToJobObject failed")

    def terminate(self) -> None:
        self._k32.TerminateJobObject(self._job, 1)

    def close(self) -> None:
        if self._job:
            self._k32.CloseHandle(self._job)
            self._job = None


class ProcessTree:
    def __init__(self, popen: subprocess.Popen[bytes], job: _WinJob | None) -> None:
        self.popen = popen
        self._job = job

    @property
    def pid(self) -> int:
        return self.popen.pid

    def kill(self) -> None:
        """Kill the child and every descendant. Safe to call twice and after the child exited."""
        if self._job is not None:
            self._job.terminate()
        elif not IS_WINDOWS:
            try:
                os.killpg(self.popen.pid, signal.SIGKILL)  # group id == pid (new session)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            self.popen.kill()
        except OSError:
            pass

    def close(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None


def spawn(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> ProcessTree:
    """Start `argv` with pipes for stdin/stdout/stderr, no shell, inside its own kill group."""
    if IS_WINDOWS:
        job = _WinJob()
        popen = subprocess.Popen(  # noqa: S603
            list(argv), cwd=cwd, env=dict(env), shell=False,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            ),
        )  # fmt: skip
        try:
            job.assign(popen)
        except OSError:
            popen.kill()
            job.close()
            raise
        return ProcessTree(popen, job)
    popen = subprocess.Popen(  # noqa: S603
        list(argv), cwd=cwd, env=dict(env), shell=False,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )  # fmt: skip
    return ProcessTree(popen, None)
