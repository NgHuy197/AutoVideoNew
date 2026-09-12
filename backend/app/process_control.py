"""Owned process trees and PID-reuse-safe termination.

Windows Job Objects provide the hard kill-on-owner-exit guarantee. On systems
where the process is already inside a restrictive parent job (common in test
hosts), assigning a nested job can fail; the supervisor then falls back to a
creation-time and command-line checked ``taskkill /T``. POSIX uses process
groups so the same tests remain portable.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .diagnostics import ProcessIdentity, identity_matches, process_identity


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _HANDLE = ctypes.wintypes.HANDLE
    _DWORD = ctypes.wintypes.DWORD
    _SIZE_T = ctypes.c_size_t

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", _DWORD),
            ("MinimumWorkingSetSize", _SIZE_T),
            ("MaximumWorkingSetSize", _SIZE_T),
            ("ActiveProcessLimit", _DWORD),
            ("Affinity", _SIZE_T),
            ("PriorityClass", _DWORD),
            ("SchedulingClass", _DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", _SIZE_T),
            ("JobMemoryLimit", _SIZE_T),
            ("PeakProcessMemoryUsed", _SIZE_T),
            ("PeakJobMemoryUsed", _SIZE_T),
        ]

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _CREATE_SUSPENDED = 0x00000004
    _kernel32.CreateJobObjectW.argtypes = [ctypes.wintypes.LPVOID, ctypes.wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = _HANDLE
    _kernel32.SetInformationJobObject.argtypes = [_HANDLE, _DWORD, ctypes.wintypes.LPVOID, _DWORD]
    _kernel32.SetInformationJobObject.restype = ctypes.wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [_HANDLE, _HANDLE]
    _kernel32.AssignProcessToJobObject.restype = ctypes.wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [_HANDLE, _DWORD]
    _kernel32.TerminateJobObject.restype = ctypes.wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [_HANDLE]
    _kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    _kernel32.ResumeThread.argtypes = [_HANDLE]
    _kernel32.ResumeThread.restype = _DWORD
    _kernel32.OpenThread.argtypes = [_DWORD, ctypes.wintypes.BOOL, _DWORD]
    _kernel32.OpenThread.restype = _HANDLE
    _kernel32.CloseHandle.argtypes = [_HANDLE]
    _kernel32.CloseHandle.restype = ctypes.wintypes.BOOL

    _THREAD_SUSPEND_RESUME = 0x0002


class WindowsJob:
    """A KILL_ON_JOB_CLOSE Windows Job Object, or a no-op portable fallback."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.handle: object | None = None
        self.assigned = False
        if os.name != "nt":
            return
        handle = _kernel32.CreateJobObjectW(None, name)
        if not handle:
            return
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        configured = _kernel32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits), ctypes.sizeof(limits))
        if not configured:
            _kernel32.CloseHandle(handle)
            return
        self.handle = handle

    @property
    def available(self) -> bool:
        return self.handle is not None

    def assign(self, process: subprocess.Popen) -> bool:
        if self.handle is None or os.name != "nt":
            return False
        process_handle = getattr(process, "_handle", None)
        if not process_handle:
            return False
        assigned = _kernel32.AssignProcessToJobObject(self.handle, process_handle)
        self.assigned = bool(assigned)
        return self.assigned

    def terminate(self, exit_code: int = 1) -> bool:
        if self.handle is None or os.name != "nt":
            return False
        return bool(_kernel32.TerminateJobObject(self.handle, exit_code))

    def close(self) -> None:
        if self.handle is not None and os.name == "nt":
            _kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "WindowsJob":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass
class OwnedProcess:
    process: subprocess.Popen
    identity: ProcessIdentity | None
    job: WindowsJob | None = None
    log_handle: object | None = None

    @property
    def pid(self) -> int:
        return int(self.process.pid)

    def is_running(self) -> bool:
        return self.process.poll() is None

    def close(self, *, terminate: bool = False) -> None:
        if terminate and self.is_running():
            terminate_owned(self)
        if self.job is not None:
            self.job.close()
        handle = self.log_handle
        if handle is not None:
            try:
                handle.close()  # type: ignore[union-attr]
            except (OSError, ValueError):
                pass
            self.log_handle = None


def spawn_owned(args: list[str], *, cwd: Path, log_path: Path, job_name: str,
                env: dict[str, str] | None = None) -> OwnedProcess:
    """Spawn a child with persisted stdout/stderr and assign its process tree."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab", buffering=0)
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # CREATE_SUSPENDED closes the small race in which a worker could launch
    # FFmpeg before it is assigned to its owning Job Object.
    if os.name == "nt":
        flags |= _CREATE_SUSPENDED
    try:
        process = subprocess.Popen(args, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                    stdout=log_handle, stderr=subprocess.STDOUT,
                                    creationflags=flags, start_new_session=(os.name != "nt"))
    except OSError:
        log_handle.close()
        raise

    job = WindowsJob(job_name) if os.name == "nt" else None
    try:
        if os.name == "nt" and job is not None:
            job.assign(process)
            # CPython keeps the process handle but closes the primary thread
            # handle returned by CreateProcess. Re-open one of the suspended
            # process's threads before releasing it; otherwise CREATE_SUSPENDED
            # would leave every worker/API permanently suspended. A process
            # handle alone cannot be passed to ResumeThread.
            try:
                import psutil
                thread_ids = [thread.id for thread in psutil.Process(process.pid).threads()]
            except Exception as exc:
                raise RuntimeError("cannot inspect suspended child thread") from exc
            resumed = False
            for thread_id in thread_ids:
                thread_handle = _kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, int(thread_id))
                if thread_handle:
                    try:
                        result = _kernel32.ResumeThread(thread_handle)
                        if result != 0xFFFFFFFF:
                            resumed = True
                            break
                    finally:
                        _kernel32.CloseHandle(thread_handle)
            if not resumed:
                raise RuntimeError("cannot resume suspended child")
        identity = process_identity(process.pid)
        return OwnedProcess(process, identity, job, log_handle)
    except BaseException:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False,
                               capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                process.kill()
        finally:
            process.wait(timeout=10)
            if job is not None:
                job.close()
            log_handle.close()
        raise


def _taskkill(pid: int) -> bool:
    result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False,
                            capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return result.returncode == 0


def terminate_owned(child: OwnedProcess, *, timeout: float = 10.0) -> bool:
    """Terminate a process tree only when the captured identity still matches."""

    if child.process.poll() is not None:
        if child.job is not None:
            child.job.close()
        return True
    current = process_identity(child.pid)
    if child.identity is not None and not identity_matches(
            current, pid=child.pid, started_at=child.identity.create_time):
        return False
    terminated = False
    if child.job is not None and child.job.available and child.job.assigned:
        terminated = child.job.terminate()
    if not terminated:
        if os.name == "nt":
            terminated = _taskkill(child.pid)
        else:
            try:
                os.killpg(os.getpgid(child.pid), signal.SIGTERM)
                terminated = True
            except (OSError, ProcessLookupError):
                terminated = True
    try:
        child.process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            terminated = _taskkill(child.pid) or terminated
        else:
            try:
                os.killpg(os.getpgid(child.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        try:
            child.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
    return terminated


def close_owned(child: OwnedProcess) -> None:
    """Close a child after it exits, releasing its Job Object and log handle."""

    child.close(terminate=False)
