"""Durable ReAct checkpoints; uncertain operations are never replayed automatically."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

_OWNER = uuid.uuid4().hex
_OWNER_LOCKS: dict[str, Any] = {}

#: A checkpoint must stay cheap to write, so a directory input is hashed up to
#: this many files. Beyond it the tree is reported ``unhashed`` (not resumable).
MAX_TREE_FILES = 2000


def _lock_owner(path: Path, *, retain: bool = False) -> bool:
    """A process-lifetime OS lock distinguishes a crash from a live worker."""
    key = str(path)
    if key in _OWNER_LOCKS:
        return False
    handle = path.open("a+b")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                handle.write(b"1")
                handle.flush()
                handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    if retain:
        _OWNER_LOCKS[key] = handle
    else:
        handle.close()
    return True


class RecoveryBlocked(ValueError):
    """Checkpoint unavailable, stale, or unsafe to continue."""


class RecoveryStore:
    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            from forgeflow.config import get_settings

            settings = get_settings()
            # Separate isolated test databases from the live service's journal.
            namespace = hashlib.sha256(str(settings.postgres_url).encode()).hexdigest()[:16]
            path = settings.resource_store_path() / "recovery" / (namespace + ".sqlite3")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _lock_owner(self.path.with_name(self.path.name + "." + _OWNER + ".owner"), retain=True)
        with self.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS checkpoints (tenant TEXT NOT NULL, actor TEXT NOT NULL, run TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (tenant,actor,run))"
            )

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA synchronous=FULL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def get(self, tenant: str, actor: str, run: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT version,status,body FROM checkpoints WHERE tenant=? AND actor=? AND run=?",
                (tenant, actor, run),
            ).fetchone()
        if row is None:
            raise RecoveryBlocked("恢复检查点不存在或不属于当前用户")
        state = json.loads(row[2])
        owner = state.get("owner", "")
        if (
            row[1] == "active"
            and isinstance(owner, str)
            and len(owner) == 32
            and all(c in "0123456789abcdef" for c in owner)
        ):
            owner_path = self.path.with_name(self.path.name + "." + owner + ".owner")
            if _lock_owner(owner_path):
                with self.connect() as conn:
                    conn.execute(
                        "UPDATE checkpoints SET status='paused',version=version+1 WHERE tenant=? AND actor=? AND run=? AND version=? AND status='active'",
                        (tenant, actor, run, row[0]),
                    )
                return self.get(tenant, actor, run)
        return {"version": row[0], "status": row[1], "state": state}

    def save(
        self, tenant: str, actor: str, run: str, state: dict, *, status: str = "active"
    ) -> int:
        if not tenant or not actor or not run:
            raise RecoveryBlocked("恢复日志缺少用户或租户")
        body = json.dumps({**state, "owner": _OWNER}, ensure_ascii=False, allow_nan=False)
        if len(body.encode()) > 8 * 1024 * 1024:
            raise RecoveryBlocked("恢复检查点过大；停止执行，避免丢失操作记录")
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO checkpoints VALUES (?,?,?,1,?,?) ON CONFLICT(tenant,actor,run) DO UPDATE SET version=version+1,status=excluded.status,body=excluded.body",
                (tenant, actor, run, status, body),
            )
            return conn.execute(
                "SELECT version FROM checkpoints WHERE tenant=? AND actor=? AND run=?",
                (tenant, actor, run),
            ).fetchone()[0]

    def claim(self, tenant: str, actor: str, run: str, version: int) -> dict:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT version,status,body FROM checkpoints WHERE tenant=? AND actor=? AND run=?",
                (tenant, actor, run),
            ).fetchone()
            if row is None or row[0] != version or row[1] != "paused":
                raise RecoveryBlocked("检查点版本过期或任务当前不可恢复")
            state = json.loads(row[2])
            if not state.get("messages") or state.get("unsupported"):
                raise RecoveryBlocked("该执行路径没有可恢复的模型检查点")
            if state.get("inflight"):
                raise RecoveryBlocked("中断操作结果待核实，禁止自动重放")
            conn.execute(
                "UPDATE checkpoints SET status='claimed',version=version+1 WHERE tenant=? AND actor=? AND run=?",
                (tenant, actor, run),
            )
            return state


def _file_digest(path: Path, *, chunk: int = 1024 * 1024, limit: int = 64 * 1024 * 1024) -> str:
    """sha256 of a file, streamed and size-capped (a 2 GB input must not OOM)."""
    digest = hashlib.sha256()
    read = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            read += len(block)
            if read > limit:
                return "unhashed:file-too-large"
            digest.update(block)
    return digest.hexdigest()


def directory_digest(path: Path, *, limit: int | None = None) -> str:
    """Content digest of a directory tree — the honest replacement for "unverified".

    Sorted relative paths plus per-file digests, so an added, removed, renamed or
    edited file all change the result. Symlinks are never followed or hashed: a
    link can point outside the approved input scope, and letting it into the
    digest would tie the checkpoint to data the run was never allowed to read.

    Bounded on purpose — file count, per-file size and any walk error return an
    ``unhashed:`` marker instead of a digest. Callers must treat those as "cannot
    be verified" (the checkpoint stays non-resumable, fail-closed), never as an
    empty directory.
    """
    entries: list[str] = []
    count = 0
    ceiling = MAX_TREE_FILES if limit is None else limit
    try:
        for root, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not Path(root, d).is_symlink())
            for name in sorted(files):
                item = Path(root, name)
                if item.is_symlink() or not item.is_file():
                    continue
                count += 1
                if count > ceiling:
                    return "unhashed:too-many-files"
                digest = _file_digest(item)
                if digest.startswith("unhashed:"):
                    return digest
                entries.append(f"{item.relative_to(path).as_posix()}:{digest}")
    except OSError:
        return "unhashed:unreadable"
    joined = "\n".join(entries).encode()
    return f"tree:{count}:{hashlib.sha256(joined).hexdigest()}"


def unsupported_reason(context: dict[str, Any], versions: dict[str, str]) -> str:
    """Why this run's checkpoint cannot be replayed ('' ⇒ it can be attempted).

    Kept in one place so the checkpoint, the resume route and the evidence probe
    all answer with the same sentence instead of three different guesses.

    ``paths`` is deliberately **not** a reason any more: every regular file input
    is hashed into ``input_versions`` and the resume path re-hashes it, so drift
    is still refused — by evidence rather than by blanket refusal. Directories
    used to be the real blocker (``directory:unverified``); they now carry a
    bounded tree digest. A code-plane task keeps its refusal: it has no model
    checkpoint to replay at all.
    """
    if context.get("repo_path"):
        return "code-plane task: no model checkpoint to replay"
    for value in versions.values():
        if str(value).startswith("unhashed:"):
            return f"input cannot be verified ({value})"
    return ""


def input_versions(scope: Any) -> dict[str, str]:
    """Only inspect input paths already authorized by input_scope."""
    versions = {}
    for key, values in scope.inherited_inputs.items():
        if not key.endswith("paths") or not isinstance(values, list):
            continue
        for value in values:
            path = Path(value)
            if path.is_file():
                versions[str(path.resolve())] = hashlib.sha256(path.read_bytes()).hexdigest()
            elif path.is_dir():
                versions[str(path.resolve())] = directory_digest(path)
    return versions


def checkpoint(
    task: Any,
    ctx: Any,
    run: str,
    messages: list,
    remaining: list,
    model_calls: int,
    *,
    inflight: bool = False,
) -> None:
    session = getattr(task, "_recovery_session", None)
    if session is None:
        return
    from langchain_core.messages import messages_to_dict

    from forgeflow.runtime.execution_budget import current_budget

    store, initial = session
    state = {
        **initial,
        "messages": messages_to_dict(messages),
        "remaining_calls": remaining,
        "model_calls": model_calls,
        "inflight": inflight or initial.get("unknown_effect", False),
        "saved_at": time.time(),
        "budget": current_budget.get().snapshot(),
        "tool_invocations": task.context.get("tool_invocations", []),
        "llm_usage": [*task.context.get("llm_usage", []), *getattr(task, "_recovery_usage", [])],
        "skill_versions": getattr(ctx, "_skill_read_versions", {}),
        "context_requests": task.context.get("llm_runtime", {}).get("context_requests", []),
        "loaded_tools": task.context.get("_loaded_tools", []),
        "tool_schema_hashes": {
            item["function"]["name"]: hashlib.sha256(
                json.dumps(item, sort_keys=True).encode()
            ).hexdigest()
            for item in task.context.get("_approved_tool_schemas", [])
        },
    }
    store.save(ctx.tenant_id, ctx.user_id, run, state)


current_recovery: ContextVar[tuple | None] = ContextVar("task_recovery", default=None)


def persist_budget() -> None:
    session = current_recovery.get()
    if session is None:
        return
    from forgeflow.runtime.execution_budget import current_budget

    store, tenant, actor, run = session
    current = store.get(tenant, actor, run)
    state = current["state"]
    state["budget"] = current_budget.get().snapshot()
    store.save(tenant, actor, run, state)
