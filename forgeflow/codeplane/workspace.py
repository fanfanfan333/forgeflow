"""Task-scoped isolated workspaces (INC25 W2, P0-8 / AC-11 / AC-12).

A code task runs inside a **copy** of its source, never in the source itself and
never in the ForgeFlow checkout. The workspace root is
``<resource_store_root>/codeplane/<tenant>/<workspace_id>`` — always outside the
project tree — so the target repository's working tree is byte-for-byte unchanged
by a task (AC-11).

Lifecycle is explicit and recorded: ``create`` → (``reuse``) → ``release``
(``destroy=False`` keeps the files for inspection; ``destroy=True`` removes them).
Every transition is appended to the workspace's ``events`` ledger and to the
manager's process-wide history, so "创建 / 回收 / 销毁" is observable (AC-12).

INC29 T01 (§7) closes the leak this leaves open: a *terminal* run recycles its
workspace (:meth:`WorkspaceManager.release_for_run`) and an opt-in TTL sweep
(:meth:`WorkspaceManager.reap_expired`, driven by
``Settings.codeplane_workspace_ttl_hours``) removes workspaces that were kept for
inspection once they age out. ``ttl <= 0`` disables the sweep entirely.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from forgeflow.config import get_settings
from forgeflow.repositories.base import utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "Workspace",
    "WorkspaceManager",
    "get_workspace_manager",
    "reset_workspace_manager",
]

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_SKIP_DIRS: frozenset[str] = frozenset(
    {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
     ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".idea"}
)


def _is_within_project(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover
        return True
    root = _PROJECT_ROOT.resolve()
    return resolved == root or root in resolved.parents


@dataclass
class Workspace:
    """One task-scoped workspace and its lifecycle ledger."""

    workspace_id: str
    run_id: str
    tenant_id: str = "default"
    path: str = ""
    branch: str = ""
    state: str = "created"       # created | active | released | destroyed
    created_at: str = ""
    released_at: str = ""
    note: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "path": self.path,
            "branch": self.branch,
            "state": self.state,
            "created_at": self.created_at,
            "released_at": self.released_at,
            "note": self.note,
            "events": [dict(e) for e in self.events],
        }


class WorkspaceManager:
    """Create / reuse / release isolated workspaces for code tasks."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        base = Path(root) if root is not None else get_settings().resource_store_path() / "codeplane"
        self._root = Path(base)
        if _is_within_project(self._root):
            raise ValueError(
                f"workspace root must be outside the ForgeFlow project tree: {self._root}"
            )
        self._by_id: dict[str, Workspace] = {}
        self._by_run: dict[str, str] = {}
        self._history: list[dict[str, Any]] = []

    @property
    def root(self) -> Path:
        return self._root

    # ---------------------------------------------------------------- #
    # Lifecycle                                                        #
    # ---------------------------------------------------------------- #
    def create(
        self,
        run_id: str,
        source: Any = None,
        *,
        tenant_id: str | None = None,
    ) -> Workspace:
        """Create an isolated workspace for ``run_id``.

        ``source`` may be a local directory path, a ``{"path": ...}`` mapping, or a
        ``{"zip": bytes}`` mapping. It is **copied into** the workspace — the source
        is never written.
        """
        # INC29 T01 (§7) — cheap, idempotent TTL sweep *before* a new workspace is
        # made, so kept-for-inspection workspaces cannot accumulate forever. A
        # reaper failure is logged, never allowed to fail this create.
        try:
            self.reap_expired()
        except Exception as exc:  # noqa: BLE001 — the reaper is best-effort
            logger.warning("workspace reap failed before create: %s", exc)
        tenant = str(tenant_id or get_settings().default_tenant_id or "default")
        workspace_id = f"ws_{str(run_id)[:8]}_{uuid.uuid4().hex[:8]}"
        path = self._root / tenant / workspace_id
        path.mkdir(parents=True, exist_ok=True)
        ws = Workspace(
            workspace_id=workspace_id,
            run_id=str(run_id),
            tenant_id=tenant,
            path=str(path),
            state="active",
            created_at=utcnow().isoformat(),
        )
        self._record(ws, "created", f"工作区已创建：{path}")

        self._populate(ws, source)
        self._init_git(ws)

        self._by_id[workspace_id] = ws
        self._by_run[str(run_id)] = workspace_id
        return ws

    def reuse(self, run_id: str) -> Workspace | None:
        """Return the most recent live workspace for ``run_id`` (``None`` if none)."""
        workspace_id = self._by_run.get(str(run_id))
        if not workspace_id:
            return None
        ws = self._by_id.get(workspace_id)
        if ws is None or ws.state == "destroyed":
            return None
        return ws

    def release(self, workspace_id: str, destroy: bool = False) -> None:
        """Release (and optionally destroy) a workspace; records the transition."""
        ws = self._by_id.get(workspace_id)
        if ws is None:
            return
        ws.released_at = utcnow().isoformat()
        if destroy:
            shutil.rmtree(ws.path, ignore_errors=True)
            ws.state = "destroyed"
            self._record(ws, "destroyed", f"工作区已销毁：{ws.path}")
        else:
            ws.state = "released"
            self._record(ws, "released", f"工作区已回收（保留文件）：{ws.path}")

    def release_for_run(self, run_id: str, *, destroy: bool = False) -> Workspace | None:
        """Release the **live** workspace bound to ``run_id`` (``None`` if none).

        Used by the run's terminal-state recycle (INC29 T01 / §7). A run with no
        live workspace — it never had one, or its workspace was already destroyed
        — returns ``None`` instead of raising, so recycling can never turn a
        finished run into a failure. ``destroy=False`` keeps the files for
        inspection; ``destroy=True`` removes them.
        """
        ws = self.reuse(run_id)
        if ws is None:
            return None
        self.release(ws.workspace_id, destroy=destroy)
        return ws

    def reap_expired(self, now: datetime | None = None) -> list[str]:
        """Destroy workspaces older than ``Settings.codeplane_workspace_ttl_hours``.

        The TTL is the **only** automatic deleter and it is strictly opt-in:
        ``ttl <= 0`` disables it entirely and this returns ``[]`` without touching
        anything. With a positive TTL, every workspace whose ``created_at`` is
        older than ``now - ttl`` and that is not already ``destroyed`` is removed
        (via :meth:`release` with ``destroy=True``) and gets a ``reaped`` ledger
        event. Returns the reaped workspace ids (empty when nothing was due).

        ``now`` is injectable for tests; it defaults to the UTC clock.
        """
        ttl = int(getattr(get_settings(), "codeplane_workspace_ttl_hours", 0) or 0)
        if ttl <= 0:
            return []
        reference = _as_utc(now) if isinstance(now, datetime) else utcnow()
        cutoff = reference - timedelta(hours=ttl)
        reaped: list[str] = []
        for ws in list(self._by_id.values()):
            if ws.state == "destroyed":
                continue
            created = _parse_iso(ws.created_at)
            if created is None or created > cutoff:
                continue
            self.release(ws.workspace_id, destroy=True)
            self._record(ws, "reaped", f"工作区已按 TTL={ttl}h 到期回收：{ws.path}")
            reaped.append(ws.workspace_id)
        return reaped

    def describe(self, run_id: str) -> dict[str, Any] | None:
        """The lifecycle record for a run's workspace (``None`` when none)."""
        ws = self.reuse(run_id) or self._by_id.get(self._by_run.get(str(run_id), ""))
        return ws.to_dict() if ws is not None else None

    def get(self, workspace_id: str) -> Workspace | None:
        return self._by_id.get(workspace_id)

    def history(self) -> list[dict[str, Any]]:
        """All recorded lifecycle events across workspaces (create/release/destroy)."""
        return [dict(e) for e in self._history]

    # ---------------------------------------------------------------- #
    # Internal                                                         #
    # ---------------------------------------------------------------- #
    def _record(self, ws: Workspace, event: str, detail: str) -> None:
        entry = {
            "workspace_id": ws.workspace_id,
            "run_id": ws.run_id,
            "event": event,
            "state": ws.state,
            "detail": detail,
            "at": utcnow().isoformat(),
        }
        ws.events.append(entry)
        self._history.append(entry)

    def _populate(self, ws: Workspace, source: Any) -> None:
        target = Path(ws.path)
        src_path = ""
        zip_bytes: bytes | None = None
        if isinstance(source, dict):
            src_path = str(source.get("path") or source.get("repo_path") or "")
            raw_zip = source.get("zip")
            if isinstance(raw_zip, (bytes, bytearray)):
                zip_bytes = bytes(raw_zip)
        elif isinstance(source, (str, os.PathLike)):
            src_path = str(source)

        if zip_bytes is not None:
            try:
                import io
                import zipfile

                with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
                    archive.extractall(target)
                self._record(ws, "populated", f"已解压 ZIP 到工作区（{len(zip_bytes)} 字节）")
            except Exception as exc:  # noqa: BLE001 — a copy failure is a note, not a crash
                ws.note = f"ZIP 内容未能解压：{exc}"
                self._record(ws, "populate_failed", ws.note)
            return

        if not src_path:
            self._record(ws, "populated", "无来源内容；已创建工作区空目录")
            return
        src = Path(src_path).expanduser()
        if not src.exists():
            ws.note = f"来源路径不存在：{src}"
            self._record(ws, "populate_failed", ws.note)
            return
        if src.is_dir():
            try:
                shutil.copytree(
                    src, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*_SKIP_DIRS),
                )
                self._record(ws, "populated", f"已复制来源目录到工作区：{src}")
            except Exception as exc:  # noqa: BLE001
                ws.note = f"来源目录复制失败：{exc}"
                self._record(ws, "populate_failed", ws.note)
        elif src.is_file():
            try:
                shutil.copy2(src, target / src.name)
                self._record(ws, "populated", f"已复制来源文件到工作区：{src}")
            except Exception as exc:  # noqa: BLE001
                ws.note = f"来源文件复制失败：{exc}"
                self._record(ws, "populate_failed", ws.note)

    def _init_git(self, ws: Workspace) -> None:
        """Initialise a git repo inside the workspace (best-effort).

        The repository is created **inside the workspace copy**, so the source
        repo's ``git status`` is untouched (AC-11). A git failure is recorded as a
        note, never raised — the engine can still run without a VCS baseline.
        """
        target = Path(ws.path)
        env = dict(os.environ)
        env.setdefault("GIT_AUTHOR_NAME", "ForgeFlow")
        env.setdefault("GIT_AUTHOR_EMAIL", "forgeflow@local")
        env.setdefault("GIT_COMMITTER_NAME", "ForgeFlow")
        env.setdefault("GIT_COMMITTER_EMAIL", "forgeflow@local")
        try:
            subprocess.run(["git", "init", "-q"], cwd=str(target), env=env,
                           capture_output=True, timeout=20, check=False)
            subprocess.run(["git", "add", "-A"], cwd=str(target), env=env,
                           capture_output=True, timeout=30, check=False)
            completed = subprocess.run(
                ["git", "commit", "-q", "-m", "forgeflow workspace baseline"],
                cwd=str(target), env=env, capture_output=True, timeout=30, check=False,
            )
            if completed.returncode == 0:
                ws.branch = _current_branch(target, env)
                self._record(ws, "git_initialized", f"工作区 git 基线已建立（branch={ws.branch or 'unknown'}）")
            else:
                ws.branch = _current_branch(target, env)
                self._record(ws, "git_baseline_empty", "工作区 git 已初始化（无可提交内容）")
        except FileNotFoundError:
            ws.note = (ws.note + "；" if ws.note else "") + "未找到 git，可执行但无 VCS 基线"
            self._record(ws, "git_unavailable", "未找到 git 可执行文件")
        except Exception as exc:  # noqa: BLE001
            ws.note = (ws.note + "；" if ws.note else "") + f"git 初始化失败：{exc}"
            self._record(ws, "git_failed", str(exc))


def _current_branch(repo: Path, env: dict[str, str]) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo), env=env, capture_output=True, text=True, timeout=10, check=False,
        )
        return (completed.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _as_utc(value: datetime) -> datetime:
    """Normalise a possibly-naive datetime to a timezone-aware UTC datetime."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _parse_iso(text: str) -> datetime | None:
    """Parse an ISO-8601 timestamp into aware UTC (``None`` when unparseable)."""
    try:
        return _as_utc(datetime.fromisoformat(str(text)))
    except (TypeError, ValueError):
        return None


_MANAGER: WorkspaceManager | None = None


def get_workspace_manager() -> WorkspaceManager:
    """Process-wide workspace manager (lazily built from ``resource_store_path``)."""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = WorkspaceManager()
    return _MANAGER


def reset_workspace_manager() -> None:
    """Drop the process-wide manager. Test helper."""
    global _MANAGER
    _MANAGER = None
