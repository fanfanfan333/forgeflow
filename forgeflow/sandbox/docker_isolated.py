"""INC46 T13 — container isolation backend (Docker) — 裁定 I.

Why this module
---------------
:mod:`forgeflow.skills.sandbox_isolated` is a **native** (pure-Python) sandbox:
on Windows + Python 3.13 without admin, kernel-level isolation is unavailable
(no ``resource`` / cgroups / network namespaces; AppContainer cannot launch a
``python`` child — ``CreateProcessW`` rc=106). The measured native report is thus
① ``landed``, ② ``degraded``, ③ ``unlanded``, ④ ``landed``, ⑤ ``degraded``,
⑥ ``degraded``, ⑦ ``landed``, ⑧ ``landed``.

Docker *is* available on this box (server ``29.8.0``, image
``python:3.12-alpine``) and its kernel primitives land the four missing pieces —
it is the **landed route** for kernel-level isolation (裁定 I / R9). This module
therefore *is* the container backend, and the native backend stays as the
**explicitly degraded** fallback (严禁静默降级): when Docker is absent the
container backend reports ``NOT_SUPPORTED`` (never a silent downgrade, never a
PASS — 红线 10).

Honesty / explicit-degradation policy
-------------------------------------
* :func:`docker_isolation_available` probes the **daemon** (``docker version`` /
  ``docker info``); tests that need Docker ``skip`` with the reason when it is
  absent and must **not** record PASS.
* :func:`run_isolated_docker` returns ``verdict="error"`` with
  ``evidence["not_supported"]=True`` when Docker is unavailable — no fallback to
  the native runner inside this function.
* The container is created with ``--rm`` (ephemeral) and a unique ``--name`` so a
  wall-clock watchdog can ``docker kill`` it deterministically.

后端能力差异表 (native vs container) — :func:`backend_capability_diff_table`
---------------------------------------------------------------------------
==========================  ============================  ===========================
能力项                       原生 sandbox_isolated          容器 docker_isolated
==========================  ============================  ===========================
① 独立子进程或容器            landed                        landed
② 默认无网络出口              degraded (解释器守卫)           landed (--network none)
③ rootfs 只读                unlanded                      landed (--read-only)
④ 独立临时工作目录销毁         landed                        landed (--rm)
⑤ 越界写 ⇒ fail              degraded (解释器守卫,无法穷举)   landed (--read-only,内核)
⑥ CPU/内存/墙钟/进程数/文件大小配额 + 超时强杀
                             degraded (内核 2× + supervisor) landed (--memory+swap / --cpus /
                                                          --pids-limit / --ulimit fsize)
⑦ env 白名单                 landed                        landed (-e 白名单)
⑧ 无生产 DB / 对象存储凭证     landed                        landed (仅显式 -e)
[附加] 文件大小配额            degraded (supervisor-only)     landed (--ulimit fsize)
==========================  ============================  ===========================
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from forgeflow.skills.sandbox_isolated import SandboxResult

__all__ = [
    "NOT_SUPPORTED",
    "DEFAULT_IMAGE",
    "DockerLimits",
    "docker_isolation_available",
    "docker_version",
    "run_isolated_docker",
    "docker_isolation_capability_report",
    "backend_capability_diff_table",
]

#: Explicit marker used when Docker is unavailable — a **skip**, never a PASS.
NOT_SUPPORTED = "NOT_SUPPORTED"

#: The pinned image the container backend runs (present on this box).
DEFAULT_IMAGE = "python:3.12-alpine"

#: stderr markers → violation code (crisp, kernel-level evidence).
_VIOLATION_MARKERS: tuple[tuple[str, str], ...] = (
    ("Read-only file system", "read_only_rootfs"),
    ("Errno 30", "read_only_rootfs"),
    ("Network is unreachable", "network_egress"),
    ("Network unreachable", "network_egress"),
    ("Errno 101", "network_egress"),
    ("BlockingIOError", "pids_limit"),
    ("Errno 11", "pids_limit"),
    ("MemoryError", "memory_oom"),
    ("Killed", "memory_oom"),
)


@dataclass
class DockerLimits:
    """The budget one container run must stay inside."""

    image: str = DEFAULT_IMAGE
    memory_mb: int = 256
    pids_limit: int = 32
    cpus: str = "1.0"
    tmpfs_mb: int = 16
    ulimit_fsize_kb: int = 8192  # 8 MB file-size ceiling (--ulimit fsize)
    timeout_s: float = 30.0
    network: str = "none"
    read_only: bool = True


# --------------------------------------------------------------------------- #
# daemon capability probe                                                       #
# --------------------------------------------------------------------------- #
def docker_version(timeout_s: float = 8.0) -> str | None:
    """The Docker **daemon** server version, or ``None`` if unreachable.

    Uses ``docker version`` (daemon round-trip) — a bare client ``docker --version``
    would report a version even with no daemon, so it is not used as the gate.
    """
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    version = (proc.stdout or "").strip()
    return version or None


def docker_isolation_available() -> bool:
    """Whether the Docker daemon is reachable (the backend's ``NOT_SUPPORTED`` gate)."""
    return docker_version() is not None


def _image_present(image: str, timeout_s: float = 8.0) -> bool:
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


# --------------------------------------------------------------------------- #
# the runner                                                                    #
# --------------------------------------------------------------------------- #
def _classify_violations(stderr: str, returncode: int | None) -> list[str]:
    """Map a container's stderr / exit code to crisp violation codes."""
    found: list[str] = []
    text = stderr or ""
    for marker, code in _VIOLATION_MARKERS:
        if marker in text and code not in found:
            found.append(code)
    if returncode == 137 and "memory_oom" not in found:
        found.append("memory_oom")  # docker OOM-kill exit code
    return found


def run_isolated_docker(
    source: str,
    *,
    limits: DockerLimits | None = None,
    extra_env: dict[str, str] | None = None,
) -> SandboxResult:
    """Execute ``source`` inside a real, kernel-isolated, throw-away container.

    **Phase 3 convergence:** this function is now a thin adapter over the
    unified sandbox runtime (:class:`forgeflow.sandbox.docker_backend.
    DockerBackend`). The container is built by the shared arg builder,
    which keeps every limit this module always enforced (``--rm``,
    ``--network none``, ``--read-only``, the **mandatory** ``--memory`` /
    ``--memory-swap`` pair, ``--pids-limit``, ``--cpus``, ``--ulimit
    fsize``, env whitelist) and closes the gaps the Phase 3 audit found
    in the old hand-rolled argv: ``--cap-drop ALL``,
    ``--security-opt no-new-privileges`` and a non-root ``--user`` are
    now always present, the workspace is a labelled per-execution bind
    mount, and the source is staged as a file inside it (was: stdin).

    Two deliberate divergences from the pre-unification behaviour, both
    hardenings: the root filesystem is read-only **even when**
    ``DockerLimits.read_only`` is False (the unified runtime never runs
    a writable rootfs), and credential-shaped ``extra_env`` entries are
    **excluded** before execution (recorded in
    ``evidence["no_prod_credentials"] = False``) instead of being
    forwarded into the container.

    Explicit degradation: when the Docker daemon is unreachable this returns
    ``verdict="error"`` with ``not_supported=True`` — it never falls back to the
    native runner and never reports a pass.

    Returns:
        A :class:`~forgeflow.skills.sandbox_isolated.SandboxResult`. A kernel
        policy breach ⇒ ``fail``; a wall-clock timeout ⇒ ``error`` (never ``pass``).
    """
    limits = limits or DockerLimits()
    result = SandboxResult(network_block=limits.network == "none", write_guard=True)

    server = docker_version()
    result.evidence["backend"] = "docker"
    result.evidence["image"] = limits.image
    result.evidence["docker_version"] = server or ""
    if server is None:
        result.evidence["docker_available"] = False
        result.evidence["not_supported"] = True
        result.evidence["reason"] = (
            f"{NOT_SUPPORTED}：Docker 守护进程不可用 —— 容器后端不可用（不静默降级、"
            "不记 PASS；如需隔离请用原生后端，其能力见 sandbox_isolated.isolation_capability_report）"
        )
        result.verdict = "error"
        return result

    result.evidence["docker_available"] = True
    result.evidence["image_present"] = _image_present(limits.image)

    from forgeflow.sandbox.docker_backend import DockerBackend
    from forgeflow.sandbox.runtime import (
        SandboxLimits,
        SandboxPolicy,
        SandboxPolicySpec,
        SandboxRequest,
        WorkspaceManager,
        sandbox_root_path,
        validate_requested_env,
    )

    # Credential-shaped entries never reach the container (the unified
    # runtime refuses them); the attempt is still recorded as evidence,
    # exactly the fact the old implementation reported.
    _credential_keys = (
        "DATABASE_URL",
        "POSTGRES_URL",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
        "S3_SECRET_KEY",
        "OPENAI_API_KEY",
    )
    result.evidence["no_prod_credentials"] = not any(
        key in (extra_env or {}) for key in _credential_keys
    )
    clean_env: dict[str, str] = {}
    for key, value in (extra_env or {}).items():
        try:
            clean_env.update(validate_requested_env({key: value}))
        except Exception:  # refused names are excluded, never forwarded
            continue

    try:
        cpu_cores = float(limits.cpus) if limits.cpus else 1.0
    except (TypeError, ValueError):
        cpu_cores = 1.0
    unified_limits = SandboxLimits(
        cpu_cores=cpu_cores,
        memory_bytes=max(1, limits.memory_mb) * 1024 * 1024,
        pids_limit=limits.pids_limit,
        disk_bytes=1024 * 1024 * 1024,  # legacy had no workspace byte quota
        file_size_bytes=max(1, limits.ulimit_fsize_kb) * 1024,
        tmpfs_bytes=max(1, limits.tmpfs_mb) * 1024 * 1024,
        timeout_s=limits.timeout_s,
    )
    if limits.network == "none":
        spec = SandboxPolicySpec(level=SandboxPolicy.WORKSPACE_WRITE)
    else:
        # The legacy contract let a caller name another docker network
        # (an explicit opt-out of egress isolation); the unified
        # spelling of that is FULL_ACCESS with open egress.
        spec = SandboxPolicySpec(level=SandboxPolicy.FULL_ACCESS, network_allowlist=("*",))
    backend = DockerBackend(
        image=limits.image,
        workspace_manager=WorkspaceManager(sandbox_root_path()),
    )
    unified = backend.execute(
        SandboxRequest(
            code=source,
            policy=spec,
            limits=unified_limits,
            tenant_id="skills-sandbox",
            run_id="",
            env=clean_env,
        )
    )

    result.duration_s = unified.duration_s
    result.returncode = unified.exit_code
    result.stdout = unified.stdout
    result.stderr = unified.stderr
    result.timed_out = unified.timed_out
    result.violations = _classify_violations(result.stderr, unified.exit_code)
    result.workdir_removed = True  # --rm + workspace cleanup ⇒ nothing remains
    result.env_isolated = True  # only whitelisted -e vars are injected

    if unified.timed_out:
        result.quota_exceeded = "timeout"
        result.verdict = "error"
    elif result.violations:
        result.verdict = "fail"
        if "memory_oom" in result.violations:
            result.quota_exceeded = "memory"
    elif unified.exit_code == 0:
        result.verdict = "pass"
    else:
        result.verdict = "fail"
    return result


# --------------------------------------------------------------------------- #
# container capability report (task-book 最低标准 → 派生 8 行)                   #
# --------------------------------------------------------------------------- #
def docker_isolation_capability_report() -> list[dict[str, str]]:
    """「真实隔离最低标准」checklist × means / status / note（**8 行**，派生展开）— **container** backend.

    **溯源（防 over-claim）**：本报告的 **8 行**是任务书 T13「真实隔离」最低标准
    （**7 条**，分号分隔，见 ``_inc46_fulltext.md:368``）的**派生展开**，*不是*任务书
    原生的 ①…⑧ 清单。逐条映射与 :func:`forgeflow.skills.sandbox_isolated.isolation_capability_report`
    完全一致::

        任务书条1「独立子进程或容器」                  → ①
        任务书条2「默认无网络出口」                    → ②
        任务书条3「只读根文件系统 + 独立临时工作目录」   → 拆为 ③ + ④
        任务书条4「CPU/内存/墙钟/进程数/文件大小配额」   → 并入 ⑥
        任务书条5「环境变量白名单」                    → ⑦
        任务书条6「无生产 DB / 对象存储凭证」           → ⑧
        任务书条7「超时强杀」                          → 并入 ⑥
        【任务书 T13 阴性探针句】「写工作目录外 ⇒ 失败」 → 提为 ⑤（7 条最低标准未单列）

        ⇒ 8 行 = 7 条 + 1 拆（条3→③④）+ 1 提（阴性探针→⑤）+ 1 并（条4、条7→⑥）

    ``status`` describes the backend *when Docker is active*; when Docker is
    unavailable every item degrades to ``NOT_SUPPORTED`` (skip, never PASS).
    """
    return [
        {
            "requirement": "① 独立子进程或容器执行",
            "status": "landed",
            "means": "docker run --rm <image> python - （独立容器进程）",
            "probe": "阳性：容器内执行真实计算，退出码/输出如实回传",
        },
        {
            "requirement": "② 默认无网络出口",
            "status": "landed",
            "means": "--network none（内核网络命名空间）",
            "probe": "阴性：TCP 外联 ⇒ OSError [Errno 101] Network is unreachable",
        },
        {
            "requirement": "③ rootfs 只读",
            "status": "landed",
            "means": "--read-only（内核 mount 级只读根）",
            "probe": "阴性：open('/x','w') ⇒ Errno 30 只读文件系统",
        },
        {
            "requirement": "④ 独立临时工作目录（执行后销毁）",
            "status": "landed",
            "means": "--rm + --tmpfs /work；容器文件系统用完即弃",
            "probe": "阳性：--rm 后无残骸（docker ps -a 无该容器）",
        },
        {
            "requirement": "⑤ 越界写 ⇒ fail（工作目录之外写被拒）",
            "status": "landed",
            "means": "--read-only（根只读）+ --tmpfs /work（可写工作区）",
            "probe": "阴性：写根 / 写 /etc ⇒ OSError [Errno 30] Read-only file system",
        },
        {
            "requirement": "⑥ CPU / 内存 / 墙钟 / 进程数 / 文件大小配额 + 超时强杀",
            "status": "landed",
            "means": "--memory Nm --memory-swap Nm（必须成对）+ --cpus + --pids-limit "
            "+ --ulimit fsize + 宿主 watchdog（docker kill）",
            "probe": "阴性：内存炸弹 ⇒ exit 137（OOM 击杀）；--cpus 限速；"
            "fork 超限 ⇒ BlockingIOError [Errno 11]；文件超限 ⇒ --ulimit fsize；"
            "超时 ⇒ docker kill，verdict=error",
            "note": "只给 --memory 不配 --memory-swap ⇒ 超配会被静默放行（本后端强制成对）",
        },
        {
            "requirement": "⑦ 环境变量白名单（不继承生产密钥）",
            "status": "landed",
            "means": "仅注入显式 -e 变量；宿主 env 不入容器",
            "probe": "阴性：宿主设 PROD_SECRET，容器内读取 = None",
        },
        {
            "requirement": "⑧ 无生产 DB / 对象存储凭证",
            "status": "landed",
            "means": "仅注入显式 -e；不注入任何 DB / 对象存储凭证"
            "（DATABASE_URL / AWS_* / S3_SECRET_KEY 等均不入容器）",
            "probe": "阴性：宿主设 DATABASE_URL / AWS_SECRET_ACCESS_KEY，容器内读取 = None",
        },
    ]


def backend_capability_diff_table() -> list[dict[str, str]]:
    """Native (pure-Python) vs container (Docker) capability difference table (裁定 I).

    Each row: ``{"能力", "native", "container", "note"}``. Rows ①…⑧ are **aligned in
    order + numbering** to :func:`forgeflow.skills.sandbox_isolated.isolation_capability_report`
    (same 8-row derived expansion of the task book's 7-clause 「真实隔离最低标准」).
    Capabilities that the report folds into ⑥ are listed **after** the eight as
    ``[附加]`` rows. This is the machine-readable form of the table in the module
    docstring.
    """
    return [
        {"能力": "① 独立子进程或容器", "native": "landed", "container": "landed", "note": ""},
        {
            "能力": "② 默认无网络出口",
            "native": "degraded",
            "container": "landed",
            "note": "原生=解释器 socket 守卫；容器 --network none（内核）",
        },
        {
            "能力": "③ rootfs 只读",
            "native": "unlanded",
            "container": "landed",
            "note": "原生无内核原语；容器 --read-only",
        },
        {
            "能力": "④ 独立临时工作目录（执行后销毁）",
            "native": "landed",
            "container": "landed",
            "note": "容器 --rm",
        },
        {
            "能力": "⑤ 越界写 ⇒ fail（工作目录之外写被拒）",
            "native": "degraded",
            "container": "landed",
            "note": "原生=解释器级守卫（builtins/io/os.open，无法穷举）；容器=--read-only（内核）",
        },
        {
            "能力": "⑥ CPU / 内存 / 墙钟 / 进程数 / 文件大小配额 + 超时强杀",
            "native": "degraded",
            "container": "landed",
            "note": "原生=内核 Job 2× 硬顶 + supervisor；容器 --memory+swap/--cpus/--pids-limit/--ulimit fsize",
        },
        {
            "能力": "⑦ 环境变量白名单（不继承生产密钥）",
            "native": "landed",
            "container": "landed",
            "note": "容器 -e 白名单",
        },
        {
            "能力": "⑧ 无生产 DB / 对象存储凭证",
            "native": "landed",
            "container": "landed",
            "note": "两者均只注入显式白名单；宿主凭证不入子进程/容器",
        },
        # --- 附加行（报告 8 行之外；报告已把它们并入 ⑥）------------------------- #
        {
            "能力": "[附加] 文件大小配额",
            "native": "degraded",
            "container": "landed",
            "note": "报告 ⑥ 已含；原生=supervisor-only；容器 --ulimit fsize",
        },
        {
            "能力": "[附加] CPU 配额",
            "native": "degraded",
            "container": "landed",
            "note": "报告 ⑥ 已含；原生=内核 Job PROCESS_TIME + supervisor；容器 --cpus",
        },
    ]
