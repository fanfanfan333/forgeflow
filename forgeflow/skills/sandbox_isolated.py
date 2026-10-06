"""INC46 T13 — real isolation sandbox (独立子进程执行 + 守卫 + 配额).

Why this module
---------------
T04's "lightweight sandbox" runs candidate tests **in-process**
(``tester._run_tests_restricted`` → ``ThreadPoolExecutor``): there is no OS
isolation and a runaway / ill-behaved test can touch the host. T13 replaces
that with a **real, out-of-process** runner: the test script executes in an
independent ``python`` subprocess, inside a throw-away temp work dir, with a
whitelisted environment (no production secrets inherited), application-level
network / write guards, and a supervising quota enforcer that hard-kills the
child on wall-clock / memory / process-count budget breaches.

Honesty (平台真相)
-----------------
This runs on **Windows + Python 3.13**, where the strong primitives do not
exist. Per the measured T13 baseline (``qa_tmp/qa_t13_isolation_baseline.txt``):

* ``resource`` / ``psutil`` — **absent** (no ``RLIMIT_*``);
* cgroup / seccomp / network-namespace — **not available**; system-level
  network blocking needs admin (we are not admin);
* Windows **AppContainer** — a profile is creatable without admin, but a
  ``python`` child **cannot launch inside it** (``CreateProcessW`` →
  ``rc=106`` — missing ACL/capability), so it is **not** a usable network /
  filesystem isolation primitive here (see the ``_t13_appcontainer`` probe);
* Windows **Job Object** quotas (``PROCESS_MEMORY`` / ``JOB_MEMORY`` /
  ``PROCESS_TIME`` / ``JOB_TIME`` / ``ACTIVE_PROCESS``) — **enforced**, with the
  *correct pose*: ``SetInformationJobObject`` is called **before** assignment
  **and** the child is launched ``CREATE_SUSPENDED`` → ``AssignProcessToJobObject``
  → ``ResumeThread`` (early binding, no escape window); the child's membership in
  *our* job is self-proved via ``QueryInformationJobObject`` (→ ``ActiveProcesses``),
  never via the empty ``IsProcessInJob(p, NULL, …)`` predicate (measured in
  ``_t13_job_reverify``). The Job Object is the *hard kernel ceiling* (2× budget)
  while the parent **supervisor thread** is the primary detector that owns the
  peak telemetry, the exact declared budget and the ``quota_exceeded`` reason.
  File-size has **no** Job Object primitive ⇒ supervisor-only.

The task book's 「真实隔离最低标准」 (v3 增补，**7 条**分号分隔：「独立子进程或容器；
默认无网络出口；只读根文件系统 + 独立临时工作目录（执行后销毁）；CPU / 内存 / 墙钟 /
进程数 / 文件大小配额；环境变量白名单（不继承生产密钥）；无生产 DB / 对象存储凭证；
超时强杀」) is **展开**为 :func:`isolation_capability_report` 的 **8 行** —— 这 8 行是
**派生展开**，*不是*任务书原生的 ①…⑧ 清单（逐条派生映射见该函数 docstring）。每个条目
标注 means / status / note, 不谎报。Per-item status on this box (Windows + Python 3.13,
no admin):

1. independent subprocess (or container) execution — ``landed``
2. no network egress by default — ``degraded`` (interpreter-level guard only — a
   grandchild that re-enters a clean interpreter escapes; kernel block needs
   admin, and AppContainer cannot launch python ⇒ no non-admin kernel route)
3. read-only rootfs — ``unlanded`` (no non-admin Windows primitive; the write
   guard is interpreter-level only — the container backend is the landed route)
4. independent temp work dir, destroyed after — ``landed``
5. write-outside-workdir ⇒ ``fail`` — ``degraded`` (interpreter-level write guard
   covering builtins.open / io.open / os.open / pathlib; cannot enumerate every
   fs-mutating primitive ⇒ partial; the container backend is the landed route)
6. CPU / memory / wall-clock / process-count / file-size quota + timeout hard-kill
   — ``degraded`` (the kernel **Job Object**
   ``PROCESS_MEMORY|JOB_MEMORY|PROCESS_TIME|JOB_TIME|ACTIVE_PROCESS`` **is** armed
   — set *before* assignment (early binding) and *measured* to kill the offender —
   but it is a coarse hard ceiling (2× budget); the *exact* declared budget is
   enforced by the parent supervisor, and file-size has **no** Job Object
   primitive ⇒ supervisor-only)
7. env-var whitelist, no ``os.environ`` inheritance / production secrets —
   ``landed``
8. no production DB / object-storage credentials — ``landed`` (the env whitelist
   excludes ``DATABASE_URL`` / ``AWS_*`` / ``OPENAI_API_KEY`` …; nothing is
   injected; ``run_isolated`` never forwards a parent credential)

「degraded」= 部分（有可测的强制点，但弱于理想原语）；「unlanded」= NOT landed,
显式不声称。Nothing is judged 「基本可用 / 大致可用」.

Red line 3: the sandbox **runs a real subprocess** and never forges a Skill
Runtime success; a timeout / quota kill is ``error``, never ``pass``.

Native vs container: this pure-Python backend cannot land the kernel-level
primitives ③ 只读根 (``unlanded``) / ② 无网络 (``degraded``) — their kernel
primitives are **NOT_SUPPORTED** without admin (``docker info`` / the
``_t13_appcontainer`` probe show no non-admin AppContainer route); the
write-outside-workdir guard ⑤ is likewise interpreter-level ⇒ ``degraded``. The
**landed** route is the container backend
:mod:`forgeflow.sandbox.docker_isolated`, which probes Docker (``docker info`` /
``docker version``); when Docker is absent that backend reports ``NOT_SUPPORTED``
(an explicit skip with a reason, never a PASS). This module deliberately does not
auto-switch backends — degradation is always explicit.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ctypes import wintypes

__all__ = [
    "VIOLATION_SENTINEL",
    "IsolationLimits",
    "SandboxResult",
    "run_isolated",
    "isolation_capability_report",
    "descendant_pids",
    "tree_memory_bytes",
    "tree_cpu_seconds",
]

#: Machine-readable marker a guard writes to ``stderr`` when a policy is broken.
VIOLATION_SENTINEL = "###FF_ISOLATION_VIOLATION###"

_IS_WINDOWS = os.name == "nt"

#: Win32 ``CREATE_SUSPENDED`` (0x00000004). Defined locally rather than via
#: ``subprocess.CREATE_SUSPENDED`` — not every Python build exposes that name.
_CREATE_SUSPENDED = 0x00000004

#: Environment keys a child process may inherit. Everything else (API keys,
#: DB URLs, cloud creds, tenant secrets…) is dropped — no ``os.environ`` copy.
_ENV_WHITELIST: tuple[str, ...] = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
)


@dataclass
class IsolationLimits:
    """The budget one isolated run must stay inside.

    Empty-declaration handling: a ``max_file_mb <= 0`` value means "no file-size
    quota declared" and *disables* the file-size check (it is **not** read as
    "zero bytes allowed", which would instantly trip on the work dir's own
    bootstrap files). The same convention applies to ``max_cpu_s <= 0`` /
    ``max_processes <= 0`` / ``max_memory_mb <= 0`` — an unset budget is no
    budget, never a degenerate zero.
    """

    timeout_s: float = 8.0
    max_memory_mb: int = 512
    max_processes: int = 6
    max_cpu_s: float = 4.0
    max_file_mb: int = 32


@dataclass
class SandboxResult:
    """The verdict of one isolated run — never collapses a failure into a pass."""

    verdict: str = "error"  # "pass" | "fail" | "error"
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    quota_exceeded: str | None = None  # "timeout"|"memory"|"processes"|None
    violations: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    workdir: str = ""
    workdir_removed: bool = False
    env_isolated: bool = False
    network_block: bool = True
    write_guard: bool = True
    quota_enabled: bool = True
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """Only a clean ``pass`` counts as success (越权 / 超时 / 配额 一律不算)."""
        return self.verdict == "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "quota_exceeded": self.quota_exceeded,
            "violations": list(self.violations),
            "duration_s": round(self.duration_s, 3),
            "workdir_removed": self.workdir_removed,
            "env_isolated": self.env_isolated,
            "network_block": self.network_block,
            "write_guard": self.write_guard,
            "quota_enabled": self.quota_enabled,
            "passed": self.passed,
        }


# --------------------------------------------------------------------------- #
# child bootstrap: the in-process guards (network + write)                     #
# --------------------------------------------------------------------------- #
_BOOTSTRAP_SRC = r'''
import builtins
import os
import runpy
import sys

WORKDIR = os.environ.get("FF_SANDBOX_WORKDIR", os.getcwd())
NETWORK_BLOCK = os.environ.get("FF_SANDBOX_NETWORK_BLOCK") == "1"
WRITE_GUARD = os.environ.get("FF_SANDBOX_WRITE_GUARD") == "1"
SENTINEL = "###FF_ISOLATION_VIOLATION###"


def _violate(kind, detail=""):
    sys.stderr.write("%s%s|%s\n" % (SENTINEL, kind, detail))
    sys.stderr.flush()


if NETWORK_BLOCK:
    import socket

    def _blocked(*args, **kwargs):
        _violate("network_egress", "socket egress blocked by sandbox")
        raise OSError("FF-SANDBOX-BLOCKED: network egress is disabled")

    socket.socket.connect = _blocked
    socket.socket.connect_ex = _blocked
    socket.create_connection = _blocked
    socket.getaddrinfo = _blocked

if WRITE_GUARD:
    import io as _io

    _root = os.path.realpath(WORKDIR)
    _real_builtins_open = builtins.open
    _real_io_open = _io.open
    _real_os_open = os.open

    def _check(target):
        """Fail-closed if ``target`` resolves outside the work dir (all entries share it)."""
        try:
            resolved = os.path.realpath(os.path.abspath(str(target)))
        except Exception:
            resolved = str(target)
        if not (resolved == _root or resolved.startswith(_root + os.sep)):
            _violate("write_outside_workdir", resolved)
            raise PermissionError("FF-SANDBOX-DENIED: write outside workdir: %s" % resolved)
        return resolved

    _write_modes = ("w", "a", "x", "+")

    def _guarded_open(file, mode="r", *args, **kwargs):
        if any(ch in str(mode) for ch in _write_modes):
            _check(file)
        return _real_builtins_open(file, mode, *args, **kwargs)

    def _guarded_io_open(file, mode="r", *args, **kwargs):
        # io.open keeps its OWN binding to the C opener; patching ``builtins.open``
        # does not affect it (they are separate namespaces), so guard it too.
        # ``pathlib.Path.open`` / ``write_text`` call ``io.open`` at call time,
        # so this also covers the pathlib entry points.
        if any(ch in str(mode) for ch in _write_modes):
            _check(file)
        return _real_io_open(file, mode, *args, **kwargs)

    # os.open carries write intent in ``flags``, not a mode string. O_RDONLY == 0,
    # so a read-only open is never treated as a write.
    _o_write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def _guarded_os_open(path, flags, mode=0o777, *, dir_fd=None):
        if flags & _o_write_flags:
            if dir_fd is not None:
                # dir_fd re-bases path resolution (relative to a directory fd), so
                # the true target cannot be resolved here ⇒ fail-closed, never allow.
                _violate("write_outside_workdir", "dir_fd=%r path=%r" % (dir_fd, path))
                raise PermissionError(
                    "FF-SANDBOX-DENIED: write outside workdir (dir_fd): %s" % (path,)
                )
            _check(path)
        return _real_os_open(path, flags, mode, dir_fd=dir_fd)

    builtins.open = _guarded_open
    _io.open = _guarded_io_open
    os.open = _guarded_os_open
    # KNOWN-UNCOVERED (an interpreter-level guard cannot enumerate every mutator;
    # this is exactly why ⑤ is ``degraded``, not ``landed``):
    #   os.fdopen (fd already open — no path; transitively covered only because the
    #     fd must come from the now-guarded os.open), os.rename/os.replace/os.remove/
    #     os.mkdir/os.makedirs, shutil.copy*/move, subprocess children, and C-extension
    #     syscalls. Kernel-grade coverage comes only from the container backend.

_target = sys.argv[1] if len(sys.argv) > 1 else "main.py"
runpy.run_path(_target, run_name="__main__")
'''


# --------------------------------------------------------------------------- #
# kernel Job-Object backstop margins (defense in depth)                        #
# --------------------------------------------------------------------------- #
# The parent-side supervisor owns the *telemetry* (peak_processes / peak_memory
# / peak_cpu) and the ``quota_exceeded`` reason; it kills at the exact budget.
# The kernel Job Object is a second, *hard* ceiling set with a margin above the
# supervisor budget so the supervisor fires first and the kernel only ever acts
# if the supervisor thread is starved/dies. Correct pose (verified on this box,
# see ``_t13_job_reverify``): limits MUST be set via ``SetInformationJobObject``
# *before* the child is assigned — a limit set afterwards is silently ignored.
def _kernel_memory_backstop_mb(max_memory_mb: int) -> int:
    """Kernel PROCESS/JOB memory ceiling (bytes) — 2× the supervisor budget."""
    return max(max_memory_mb * 2, max_memory_mb + 256)


def _kernel_process_backstop(max_processes: int) -> int:
    """Kernel ACTIVE_PROCESS ceiling — comfortably above the supervisor budget."""
    return max_processes * 2 + 8


def _kernel_cpu_backstop_ticks(max_cpu_s: float) -> int:
    """Kernel PROCESS/JOB CPU ceiling (100-ns LARGE_INTEGER) — 2× the budget."""
    backstop_s = max(max_cpu_s * 2.0, max_cpu_s + 0.5)
    return int(backstop_s * 10_000_000)


# --------------------------------------------------------------------------- #
# Windows process-tree introspection (ctypes) — real, not simulated            #
# --------------------------------------------------------------------------- #
if _IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        _psapi = ctypes.WinDLL("psapi")
    except OSError:  # pragma: no cover — psapi ships with Windows
        _psapi = _kernel32

    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.OpenThread.restype = wintypes.HANDLE

    _TH32CS_SNAPPROCESS = 0x00000002
    _TH32CS_SNAPTHREAD = 0x00000004
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _THREAD_SUSPEND_RESUME = 0x0002
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _RESUME_FAILED = 0xFFFFFFFF  # (DWORD)-1 — ResumeThread error sentinel
    # Job Object limit flags (MSDN winnt.h). CPU-time limits are LARGE_INTEGER
    # 100-ns units set on the *basic* limit struct, before assignment.
    _JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002  # PerProcessUserTimeLimit
    _JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004  # PerJobUserTimeLimit
    _JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008  # ActiveProcessLimit
    _JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100  # ProcessMemoryLimit
    _JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200  # JobMemoryLimit
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
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

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        """``QueryInformationJobObject`` class 1 — used for the ``ActiveProcesses``
        self-proof (proves a pid really belongs to *our* job handle)."""

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

    class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    def _process_children_map() -> dict[int, list[int]]:
        """``{parent_pid: [child_pid, ...]}`` from a live process snapshot."""
        children: dict[int, list[int]] = {}
        snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snapshot == wintypes.HANDLE(-1).value or not snapshot:
            return children
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            more = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                children.setdefault(int(entry.th32ParentProcessID), []).append(
                    int(entry.th32ProcessID)
                )
                more = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snapshot)
        return children

    def _pid_memory_bytes(pid: int) -> int:
        handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return 0
        try:
            counters = _PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
            fn = getattr(_psapi, "GetProcessMemoryInfo", None) or getattr(
                _kernel32, "K32GetProcessMemoryInfo", None
            )
            if fn is not None and fn(handle, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
            return 0
        finally:
            _kernel32.CloseHandle(handle)

    class _FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    def _pid_cpu_seconds(pid: int) -> float:
        """User+kernel CPU seconds for one pid (0.0 on failure)."""
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return 0.0
        try:
            created, exited, kernel, user = (
                _FILETIME(),
                _FILETIME(),
                _FILETIME(),
                _FILETIME(),
            )
            if not _kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return 0.0

            def _ticks(filet: _FILETIME) -> int:
                return (int(filet.dwHighDateTime) << 32) | int(filet.dwLowDateTime)

            return (_ticks(kernel) + _ticks(user)) / 10_000_000
        finally:
            _kernel32.CloseHandle(handle)

    def _create_job(limits: IsolationLimits | None = None) -> int:
        """Create a Job Object, optionally with a kernel quota backstop.

        ``limits`` is applied **before** the child is assigned (the correct
        pose). It sets a hard kernel ceiling on memory / CPU-time / active
        processes, sized above the supervisor budget (see the margin helpers).
        ``file`` size has no Job Object primitive, so it stays supervisor-only.
        Without ``limits`` the job only carries KILL_ON_JOB_CLOSE (tree kill).
        """
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return 0
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if limits is not None:
            mem_bytes = _kernel_memory_backstop_mb(limits.max_memory_mb) * 1024 * 1024
            flags |= (
                _JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | _JOB_OBJECT_LIMIT_JOB_MEMORY
                | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                | _JOB_OBJECT_LIMIT_PROCESS_TIME
                | _JOB_OBJECT_LIMIT_JOB_TIME
            )
            ticks = _kernel_cpu_backstop_ticks(limits.max_cpu_s)
            info.BasicLimitInformation.PerProcessUserTimeLimit = ticks
            info.BasicLimitInformation.PerJobUserTimeLimit = ticks
            info.BasicLimitInformation.ActiveProcessLimit = _kernel_process_backstop(
                limits.max_processes
            )
            info.ProcessMemoryLimit = mem_bytes
            info.JobMemoryLimit = mem_bytes
        info.BasicLimitInformation.LimitFlags = flags
        _kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        return int(job)

    def _assign_job(job: int, pid: int) -> bool:
        if not job:
            return False
        handle = _kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            return bool(_kernel32.AssignProcessToJobObject(job, handle))
        finally:
            _kernel32.CloseHandle(handle)

    def _terminate_job(job: int) -> None:
        if job:
            _kernel32.TerminateJobObject(job, 1)

    def _terminate_pid(pid: int) -> None:
        handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if handle:
            _kernel32.TerminateProcess(handle, 1)
            _kernel32.CloseHandle(handle)

    def _resume_main_thread(pid: int) -> bool:
        """Resume the main thread of a ``CREATE_SUSPENDED`` child.

        The *early-binding* pose: launch the child suspended → assign it to the
        Job Object (so the kernel ceiling is armed) → resume it, leaving no escape
        window in which the child could spawn work before the quota is in force.
        Returns True when a thread was actually resumed.
        """
        snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if snapshot == wintypes.HANDLE(-1).value or not snapshot:
            return False
        resumed = False
        try:
            entry = _THREADENTRY32()
            entry.dwSize = ctypes.sizeof(_THREADENTRY32)
            more = _kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if int(entry.th32OwnerProcessID) == pid:
                    thread = _kernel32.OpenThread(
                        _THREAD_SUSPEND_RESUME, False, int(entry.th32ThreadID)
                    )
                    if thread:
                        if _kernel32.ResumeThread(thread) != _RESUME_FAILED:
                            resumed = True
                        _kernel32.CloseHandle(thread)
                    break
                more = _kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snapshot)
        return resumed

    def _job_active_processes(job: int) -> int:
        """``ActiveProcesses`` of **our own** job handle (0 when unavailable).

        This is the self-proof that the isolated child really lives inside the Job
        Object we armed — the empty ``IsProcessInJob(proc, NULL, …)`` predicate
        (which only answers "is it in *some* job") is deliberately not used.
        """
        if not job:
            return 0
        info = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        ok = _kernel32.QueryInformationJobObject(
            job,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        )
        return int(info.ActiveProcesses) if ok else 0

else:  # pragma: no cover — non-Windows fallback (POSIX)
    def _process_children_map() -> dict[int, list[int]]:
        return {}

    def _pid_memory_bytes(pid: int) -> int:  # noqa: ARG001
        return 0

    def _pid_cpu_seconds(pid: int) -> float:  # noqa: ARG001
        return 0.0

    def _create_job(limits: IsolationLimits | None = None) -> int:  # noqa: ARG001
        return 0

    def _assign_job(job: int, pid: int) -> bool:  # noqa: ARG001
        return False

    def _terminate_job(job: int) -> None:  # noqa: ARG001
        return None

    def _terminate_pid(pid: int) -> None:
        try:
            os.kill(pid, 9)
        except OSError:
            pass

    def _resume_main_thread(pid: int) -> bool:  # noqa: ARG001
        return True

    def _job_active_processes(job: int) -> int:  # noqa: ARG001
        return 0


def quotas_actually_enforced() -> bool:
    """Whether this host can really enforce the declared process / memory / CPU quotas.

    This is a **capability report, not a platform shortcut**, and the distinction
    matters. Every measurement the supervisor trips a quota on comes through the
    helpers defined above, and on the POSIX branch those are stubs:

      * ``_pid_memory_bytes`` returns ``0`` and ``_pid_cpu_seconds`` returns
        ``0.0`` forever, so no declared memory / CPU limit can ever be exceeded;
      * ``_process_children_map`` returns ``{}``, so ``descendant_pids`` only ever
        sees the child itself and ``peak_processes`` cannot rise above 1;
      * ``_create_job`` returns ``0`` and ``_assign_job`` returns ``False``, so
        there is no kernel ceiling at all and ``kernel_job_quota`` is ``False``.

    Consequence, observed on the Linux CI runner: every bomb diagnosed above is
    eventually stopped by the wall-clock timeout, so ``quota_exceeded`` comes
    back as ``"timeout"`` rather than ``"processes"`` / ``"memory"`` / ``"cpu"``.
    That is the sandbox telling the truth — it falls back to its own timeout and
    never reports a quota hit it did not measure.

    Skipping a test on a false result is therefore honest: it skips a
    *measurement that cannot be made on this host*, not an inconvenient
    platform. If POSIX quota enforcement ever lands (``resource.setrlimit``
    through a ``preexec_fn``, cgroup v2 limits, and reading real telemetry back
    out of ``resource.getrusage``), flip this for that branch to ``True`` and
    every gated test re-arms automatically.
    """
    return _IS_WINDOWS


def descendant_pids(root_pid: int) -> list[int]:
    """``root_pid`` plus every live descendant (breadth-first, deduped)."""
    children = _process_children_map()
    seen: list[int] = [root_pid]
    stack = [root_pid]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in seen:
                seen.append(child)
                stack.append(child)
    return seen


def tree_memory_bytes(pids: list[int]) -> int:
    """Sum of working-set bytes across ``pids`` (best-effort, 0 on failure)."""
    return sum(_pid_memory_bytes(pid) for pid in pids)


def tree_cpu_seconds(pids: list[int]) -> float:
    """Sum of user+kernel CPU seconds across ``pids`` (0.0 off Windows)."""
    return sum(_pid_cpu_seconds(pid) for pid in pids)


def _dir_size_bytes(path: str) -> int:
    """Total bytes of every file under ``path`` (best-effort)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


# --------------------------------------------------------------------------- #
# supervisor — the real quota enforcer (parent-side, measurable)               #
# --------------------------------------------------------------------------- #
class _Supervisor:
    """Poll the child tree and kill it the moment a budget is breached."""

    def __init__(
        self, root_pid: int, limits: IsolationLimits, kill, workdir: str = ""
    ) -> None:
        self.root_pid = root_pid
        self.limits = limits
        self._kill = kill
        self.workdir = workdir
        self.reason: str | None = None
        self.peak_processes = 0
        self.peak_memory_bytes = 0
        self.peak_cpu_s = 0.0
        self.peak_file_bytes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        start = time.monotonic()
        interval = 0.05
        while not self._stop.is_set():
            pids = descendant_pids(self.root_pid)
            self.peak_processes = max(self.peak_processes, len(pids))
            memory = tree_memory_bytes(pids)
            self.peak_memory_bytes = max(self.peak_memory_bytes, memory)
            cpu = tree_cpu_seconds(pids)
            self.peak_cpu_s = max(self.peak_cpu_s, cpu)
            file_bytes = _dir_size_bytes(self.workdir) if self.workdir else 0
            self.peak_file_bytes = max(self.peak_file_bytes, file_bytes)

            # A budget ≤ 0 is an *empty declaration* ⇒ that quota is disabled
            # (never read as a degenerate zero-limit that trips immediately).
            if self.limits.max_processes > 0 and len(pids) > self.limits.max_processes:
                self.reason = "processes"
            elif (
                self.limits.max_memory_mb > 0
                and memory > self.limits.max_memory_mb * 1024 * 1024
            ):
                self.reason = "memory"
            elif self.limits.max_cpu_s > 0 and cpu > self.limits.max_cpu_s:
                self.reason = "cpu"
            elif (
                self.limits.max_file_mb > 0
                and file_bytes > self.limits.max_file_mb * 1024 * 1024
            ):
                self.reason = "file"
            elif (
                self.limits.timeout_s > 0
                and time.monotonic() - start > self.limits.timeout_s
            ):
                self.reason = "timeout"

            if self.reason is not None:
                self._kill()
                return
            time.sleep(interval)


# --------------------------------------------------------------------------- #
# the runner                                                                   #
# --------------------------------------------------------------------------- #
def _child_env(workdir: str, *, network_block: bool, write_guard: bool,
               extra_env: dict[str, str] | None) -> dict[str, str]:
    """Build an explicit child environment — never a copy of ``os.environ``."""
    env = {key: os.environ[key] for key in _ENV_WHITELIST if key in os.environ}
    # Independent temp dir: point TEMP/TMP at the sandbox work dir.
    env["TEMP"] = workdir
    env["TMP"] = workdir
    env["FF_SANDBOX_WORKDIR"] = workdir
    env["FF_SANDBOX_NETWORK_BLOCK"] = "1" if network_block else "0"
    env["FF_SANDBOX_WRITE_GUARD"] = "1" if write_guard else "0"
    for key, value in (extra_env or {}).items():
        env[str(key)] = str(value)
    return env


def _parse_violations(stderr: str) -> list[str]:
    found: list[str] = []
    for line in (stderr or "").splitlines():
        if VIOLATION_SENTINEL in line:
            kind = line.split(VIOLATION_SENTINEL, 1)[1].split("|", 1)[0].strip()
            if kind and kind not in found:
                found.append(kind)
    return found


def run_isolated(
    source: str,
    *,
    limits: IsolationLimits | None = None,
    extra_env: dict[str, str] | None = None,
    network_block: bool = True,
    write_guard: bool = True,
    quota: bool = True,
    workdir_root: str | None = None,
    script_name: str = "main.py",
) -> SandboxResult:
    """Execute ``source`` in a real, quota-bounded, throw-away sandbox.

    Args:
        source: the Python source of the test script to run.
        limits: the budget (:class:`IsolationLimits`); defaults are used if None.
        extra_env: extra environment variables exposed to the child (whitelist
            is applied first; the parent's ``os.environ`` is **not** inherited).
        network_block: install the interpreter-level network guard.
        write_guard: install the interpreter-level write-outside-workdir guard.
        quota: run the supervising quota enforcer (wall-clock/memory/processes).
        workdir_root: parent dir for the temp work dir (defaults to the system
            temp). It is created fresh and **always destroyed** afterwards.
        script_name: file name the source is written to inside the work dir.

    Returns:
        A :class:`SandboxResult`. A policy violation ⇒ ``fail``; a timeout /
        quota kill ⇒ ``error`` (never ``pass``).
    """
    limits = limits or IsolationLimits()
    result = SandboxResult(
        network_block=network_block,
        write_guard=write_guard,
        quota_enabled=quota,
    )

    workdir = tempfile.mkdtemp(prefix="ff_sandbox_", dir=workdir_root)
    result.workdir = workdir
    job = 0
    supervisor: _Supervisor | None = None
    proc: subprocess.Popen[str] | None = None
    started = time.monotonic()
    try:
        with open(os.path.join(workdir, script_name), "w", encoding="utf-8") as fh:
            fh.write(source)
        with open(os.path.join(workdir, "__bootstrap__.py"), "w", encoding="utf-8") as fh:
            fh.write(_BOOTSTRAP_SRC)

        env = _child_env(
            workdir,
            network_block=network_block,
            write_guard=write_guard,
            extra_env=extra_env,
        )
        # 能力清单 ⑧ 自证：无生产 DB / 对象存储凭证。_ENV_WHITELIST 是显式白名单
        # （从不复制 os.environ），故任何生产凭证都不会出现在子进程 env 中。
        _credential_keys = (
            "FF_PROD_SECRET",
            "DATABASE_URL",
            "POSTGRES_URL",
            "OPENAI_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_ACCESS_KEY_ID",
            "S3_SECRET_KEY",
        )
        result.env_isolated = not any(key in env for key in _credential_keys)
        result.evidence["no_prod_credentials"] = result.env_isolated

        # Early binding (Windows): launch the child **suspended** so it can do no
        # work — and spawn no grandchild — before the Job Object ceiling is armed;
        # assign it, then resume. Off Windows the flag is simply 0 (no-op).
        suspend = bool(quota and _IS_WINDOWS)
        proc = subprocess.Popen(  # noqa: S603 — sys.executable is trusted
            [sys.executable, "__bootstrap__.py", script_name],
            cwd=workdir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=_CREATE_SUSPENDED if suspend else 0,
        )

        job = _create_job(limits if quota else None)
        assigned = _assign_job(job, proc.pid)
        result.evidence["kernel_job_quota"] = bool(quota and job)
        result.evidence["job_assigned"] = bool(assigned)
        # Self-proof via our **own** job handle (never IsProcessInJob(NULL,…)):
        # wait briefly for the accounting to reflect the assigned child.
        active = 0
        if job:
            for _ in range(10):
                active = _job_active_processes(job)
                if active > 0:
                    break
                time.sleep(0.02)
        result.evidence["job_active_processes"] = active
        resumed = _resume_main_thread(proc.pid) if suspend else False
        result.evidence["early_binding"] = bool(suspend and assigned and resumed)

        def _kill() -> None:
            _terminate_job(job)
            if proc is not None and proc.poll() is None:
                proc.kill()

        if quota:
            supervisor = _Supervisor(proc.pid, limits, _kill, workdir)
            supervisor.start()

        # Hard backstop: never let a run outlive its wall-clock budget, even
        # when the supervisor is disabled (counterfactual) or misses a beat.
        backstop = limits.timeout_s + 3.0
        backstop_fired = False
        try:
            stdout, stderr = proc.communicate(timeout=backstop)
        except subprocess.TimeoutExpired:
            backstop_fired = True
            _kill()
            stdout, stderr = proc.communicate()

        result.duration_s = time.monotonic() - started
        result.returncode = proc.returncode
        result.stdout = stdout or ""
        result.stderr = stderr or ""
        result.violations = _parse_violations(result.stderr)

        if supervisor is not None:
            if supervisor.reason == "timeout":
                result.timed_out = True
                result.quota_exceeded = "timeout"
            elif supervisor.reason in ("memory", "processes", "cpu", "file"):
                result.quota_exceeded = supervisor.reason
            result.evidence["peak_processes"] = supervisor.peak_processes
            result.evidence["peak_memory_mb"] = round(
                supervisor.peak_memory_bytes / 1024 / 1024, 1
            )
            result.evidence["peak_cpu_s"] = round(supervisor.peak_cpu_s, 2)
            result.evidence["peak_file_mb"] = round(
                supervisor.peak_file_bytes / 1024 / 1024, 2
            )

        # A wall-clock backstop that fired is *always* an (unfinished) timeout,
        # whether or not the supervisor was the one to notice it first.
        if backstop_fired and not result.timed_out and result.quota_exceeded is None:
            result.timed_out = True
            result.quota_exceeded = "timeout"

        if result.violations:
            result.verdict = "fail"
        elif result.timed_out or result.quota_exceeded:
            result.verdict = "error"
        elif proc.returncode == 0:
            result.verdict = "pass"
        else:
            result.verdict = "fail"

        result.evidence.setdefault("limits", {
            "timeout_s": limits.timeout_s,
            "max_memory_mb": limits.max_memory_mb,
            "max_processes": limits.max_processes,
            "max_cpu_s": limits.max_cpu_s,
            "max_file_mb": limits.max_file_mb,
        })
        result.evidence["workdir"] = workdir
        return result
    finally:
        if supervisor is not None:
            supervisor.stop()
        if job:
            _terminate_job(job)
            if _IS_WINDOWS:
                _kernel32.CloseHandle(job)
        if proc is not None and proc.poll() is None:
            proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)
        result.workdir_removed = not os.path.exists(workdir)


# --------------------------------------------------------------------------- #
# isolation capability checklist (honest, per-item)                            #
# --------------------------------------------------------------------------- #
def isolation_capability_report() -> list[dict[str, str]]:
    """「真实隔离最低标准」checklist × means / status / note（**8 行**，派生展开）.

    **溯源（防 over-claim）**：本报告的 **8 行**是任务书 T13「真实隔离」最低标准
    （**7 条**，分号分隔，见 ``_inc46_fulltext.md:368``）的**派生展开**，*不是*任务书
    原生的 ①…⑧ 清单。逐条映射如下::

        任务书条1「独立子进程或容器」                  → ①
        任务书条2「默认无网络出口」                    → ②
        任务书条3「只读根文件系统 + 独立临时工作目录」   → 拆为 ③ + ④
        任务书条4「CPU/内存/墙钟/进程数/文件大小配额」   → 并入 ⑥
        任务书条5「环境变量白名单」                    → ⑦
        任务书条6「无生产 DB / 对象存储凭证」           → ⑧
        任务书条7「超时强杀」                          → 并入 ⑥
        【任务书 T13 阴性探针句】「写工作目录外 ⇒ 失败」 → 提为 ⑤（7 条最低标准未单列）

        ⇒ 8 行 = 7 条 + 1 拆（条3→③④）+ 1 提（阴性探针→⑤）+ 1 并（条4、条7→⑥）

    ``status`` 三态：``landed``（= PASS）/ ``degraded``（= 部分）/
    ``unlanded``（= NOT landed）。nothing is judged 「基本可用」, and NOT landed is
    never written as 「部分」.
    """
    return [
        {
            "requirement": "① 独立子进程或容器执行",
            "status": "landed",
            "means": "subprocess.Popen([sys.executable, __bootstrap__.py])（容器路线见 docker_isolated）",
            "probe": "阳性：子进程 pid 与父进程不同；退出码如实回传",
        },
        {
            "requirement": "② 默认无网络出口",
            "status": "degraded",
            "means": "子解释器 socket 守卫（connect/create_connection/getaddrinfo 抛错）",
            "probe": "阴性：外联脚本 ⇒ violations 含 network_egress, verdict=fail",
            "note": "非内核级；未打补丁的孙进程可逃逸；内核阻断需管理员：本机 AppContainer "
            "profile 可建但 python 子进程无法在其内启动（CreateProcessW rc=106），"
            "故非管理员无内核级网络隔离原语（唯一可行路径为容器/命名空间，见 docker_isolated）",
        },
        {
            "requirement": "③ rootfs 只读",
            "status": "unlanded",
            "means": "无：Windows 无 rootfs 只读原语；icacls DENY 仅为**目录级 ACL**，非 mount 级 RO",
            "probe": "未验证（NOT-VERIFIED）：仅以 write-guard 提供「workdir 之外写 ⇒ fail」的近似",
            "note": "本机不提权无法实现系统级只读根；不谎报为已落地（容器后端 docker_isolated 是落地路线）",
        },
        {
            "requirement": "④ 独立临时工作目录（执行后销毁）",
            "status": "landed",
            "means": "tempfile.mkdtemp(); TEMP/TMP 指向该目录; finally shutil.rmtree",
            "probe": "阳性：执行后 workdir_removed=True 且目录不存在",
        },
        {
            "requirement": "⑤ 越界写 ⇒ fail（工作目录之外写被拒）",
            "status": "degraded",
            "means": "子解释器级写守卫，覆盖 builtins.open / io.open / os.open（按写入意图 "
            "flags=O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_APPEND）/ pathlib.Path.open（经 io.open）",
            "probe": "阴性：builtins.open / io.open / os.open 向 workdir 之外写 ⇒ "
            "violations 含 write_outside_workdir, verdict=fail；宿主目标文件不生成",
            "note": "【已覆盖】builtins.open / io.open / pathlib.Path.open / os.open"
            "（写意图 flags 任一，dir_fd≠None 时 fail-closed）。"
            "【已知未覆盖】os.fdopen（无路径；仅因 fd 必来自已守的 os.open 而间接受限）、"
            "os.rename/os.replace/os.remove/os.mkdir/os.makedirs、shutil.copy*/move、"
            "subprocess 子进程、C 扩展直呼系统调用 —— 解释器级守卫**本质上不可能穷举**所有"
            "文件系统变更原语 ⇒ 判 partial；内核级路线 = 容器后端（docker_isolated, landed）",
        },
        {
            "requirement": "⑥ CPU / 内存 / 墙钟 / 进程数 / 文件大小配额 + 超时强杀",
            "status": "degraded",
            "means": "内核 Job Object PROCESS_MEMORY|JOB_MEMORY|PROCESS_TIME|JOB_TIME|"
            "ACTIVE_PROCESS（早绑定：CREATE_SUSPENDED→AssignProcessToJobObject→ResumeThread）"
            "+ 父进程 supervisor 轮询进程树（GetProcessMemoryInfo + GetProcessTimes）"
            "+ 墙钟兜底强杀（communicate timeout → kill）",
            "probe": "阴性：内存 / CPU / 进程数 / 文件大小炸弹 ⇒ quota_exceeded 命中, verdict=error；"
            "超时 ⇒ timed_out=True 且 verdict=error（非 pass）；"
            "内核自证：自有 job handle 的 QueryInformationJobObject(ActiveProcesses) ≥ 1",
            "note": "已实测内核**确实会**击杀越界者（更正早先「仅 backstop」的低报）；但内核上限为"
            "预算 2× 的粗粒度硬顶，**精确**预算由 supervisor 承担 ⇒ 判「部分」。"
            "文件大小无 Job Object 原语 ⇒ 仅由 supervisor 承担",
        },
        {
            "requirement": "⑦ 环境变量白名单（不继承生产密钥）",
            "status": "landed",
            "means": "显式 _ENV_WHITELIST；父进程 secret 不入子进程",
            "probe": "阴性：父进程设 FF_PROD_SECRET，子进程读取 = None",
        },
        {
            "requirement": "⑧ 无生产 DB / 对象存储凭证",
            "status": "landed",
            "means": "env 白名单显式不含 DB / 对象存储凭证（DATABASE_URL / AWS_* / "
            "OPENAI_API_KEY 等均不入子进程）；run_isolated 不传任何生产凭证",
            "probe": "阴性：父进程设 DATABASE_URL / AWS_SECRET_ACCESS_KEY，子进程读取 = None"
            "（env_isolated=True）",
        },
    ]
