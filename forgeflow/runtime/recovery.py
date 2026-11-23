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

#: A deterministic run has no model messages; its checkpoint is the *plan*.
#: Progress is written before and after every step so a crash leaves either
#: "step N may have landed" (refused) or "next is N+1" (resumable).
PLAN_PROGRESS_KEY = "plan_progress"
MAX_PROGRESS_STEPS = 40
MAX_PROGRESS_BYTES = 256 * 1024


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


def _member(member: Any, key: str) -> str:
    if isinstance(member, dict):
        return str(member.get(key) or "")
    return str(getattr(member, key, "") or "")


def plan_signature(members: list[Any]) -> str:
    """Identity of the *executed sequence*, not of its prose.

    Two plans whose tools appear in the same order are the same sequence even if
    their notes were re-worded; a reordered, re-typed or shortened plan is a
    different one. Compared for equality only, never used as a storage key, so
    a truncated digest is enough and keeps the checkpoint small.
    """
    payload = [
        {
            "step_id": _member(member, "step_id"),
            "tool": _member(member, "tool"),
            "step_type": _member(member, "step_type"),
        }
        for member in members
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]


def plan_progress(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """The plan progress block, or ``None`` when this checkpoint has none."""
    if not isinstance(state, dict):
        return None
    progress = state.get(PLAN_PROGRESS_KEY)
    if not isinstance(progress, dict) or not progress.get("signature"):
        return None
    return progress


def resumable(state: dict[str, Any] | None) -> bool:
    """Whether this checkpoint may be claimed — the **single** predicate.

    ``claim()`` (the store) and ``GET /runs/{id}/recovery`` (the route) both ask
    this function, so "the UI says it can resume" and "the store allows a claim"
    cannot drift apart. Two independent ways to be resumable: a model message
    trail (react), or a plan progress block that has actually advanced past a
    step (deterministic). Nothing else counts — an empty checkpoint is refused.
    """
    if not isinstance(state, dict):
        return False
    if state.get("unsupported") or state.get("inflight"):
        return False
    if state.get("messages"):
        return True
    progress = plan_progress(state)
    if progress is None:
        return False
    return int(progress.get("next_index") or 0) > 0


def _bounded_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [dict(item) for item in list(steps)[:MAX_PROGRESS_STEPS]]
    try:
        size = len(json.dumps(rows, ensure_ascii=False, default=str).encode())
    except (TypeError, ValueError):
        size = MAX_PROGRESS_BYTES + 1
    if size <= MAX_PROGRESS_BYTES:
        return rows
    for row in rows:
        if row.get("observation") is not None:
            row["observation"] = {
                "truncated": True,
                "note": "observation exceeded the checkpoint budget",
            }
    return rows


def _record_projection(record: dict[str, Any]) -> dict[str, Any]:
    """Shape-preserving, payload-free view of an L2 record.

    Only used when the real trail is too large for a checkpoint. The record
    stays identifiable (tool/status/executed/result_ref) but its payload is
    flagged rather than silently dropped, so a resumed report cannot claim to
    still hold detail it no longer has.
    """
    projected = {key: value for key, value in record.items() if key != "payload"}
    projected["payload"] = {
        "truncated": True,
        "note": "record payload exceeded the checkpoint budget",
    }
    return projected


def plan_checkpoint(
    task: Any,
    run: str,
    *,
    signature: str,
    steps: list[dict[str, Any]],
    next_index: int,
    inflight: bool,
) -> None:
    """Persist step-level progress for a deterministic plan.

    Written **before** a step runs (``inflight=True``) and again once it is
    recorded. That ordering is the whole guarantee: a crash during step N leaves
    ``inflight`` set, which refuses an automatic replay; a crash between steps
    leaves ``next_index=N+1`` and a verified input digest, which is a resume we
    can defend. Checkpointing never fails the run — but when the trail cannot be
    made to fit, the safety fact (inflight / next_index) is kept and the trail is
    replaced by an explicitly marked projection.
    """
    session = current_recovery.get()
    if session is None:
        return
    store, tenant, actor, recovery_run = session
    if not recovery_run or recovery_run != run:
        return
    try:
        state = store.get(tenant, actor, run)["state"]
    except RecoveryBlocked:
        return
    state[PLAN_PROGRESS_KEY] = {
        "signature": signature,
        "next_index": max(0, int(next_index)),
        "steps": _bounded_steps(steps),
        "updated_at": time.time(),
    }
    state["inflight"] = bool(inflight)
    records = list(task.context.get("tool_invocations") or [])
    state["tool_invocations"] = records
    state.pop("records_projected", None)
    try:
        store.save(tenant, actor, run, state)
    except RecoveryBlocked:
        state["tool_invocations"] = [_record_projection(item) for item in records]
        state["records_projected"] = True
        store.save(tenant, actor, run, state)


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
            row[1] in {"active", "claimed"}
            and isinstance(owner, str)
            and len(owner) == 32
            and all(c in "0123456789abcdef" for c in owner)
        ):
            owner_path = self.path.with_name(self.path.name + "." + owner + ".owner")
            if _lock_owner(owner_path):
                with self.connect() as conn:
                    conn.execute(
                        "UPDATE checkpoints SET status='paused',version=version+1 WHERE tenant=? AND actor=? AND run=? AND version=? AND status IN ('active','claimed')",
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
            if state.get("inflight"):
                raise RecoveryBlocked("中断操作结果待核实，禁止自动重放")
            if not resumable(state):
                raise RecoveryBlocked("该执行路径没有可恢复的检查点（无模型消息，也无计划进度）")
            conn.execute(
                "UPDATE checkpoints SET status='claimed',version=version+1,body=? WHERE tenant=? AND actor=? AND run=?",
                (json.dumps({**state, "owner": _OWNER}, ensure_ascii=False, allow_nan=False), tenant, actor, run),
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


def _inside(root: Path, item: Path) -> bool:
    """Whether *item* still resolves under *root* (junctions, links, mounts).

    A refused entry is skipped, never followed: its content may live outside the
    input scope the run was authorised for, and folding it into the digest would
    tie the checkpoint to data the run was never allowed to read. An entry that
    cannot even be resolved is treated the same way.
    """
    try:
        resolved = item.resolve()
    except OSError:
        return False
    try:
        resolved.relative_to(root)
    except ValueError:
        return False
    return True


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
        root_resolved = path.resolve()
        for current, dirs, files in os.walk(path, followlinks=False):
            # ``is_symlink`` is not the only way out of the approved scope: a
            # Windows junction is a reparse point that ``is_symlink`` may miss,
            # and a host/sandbox may materialise links differently. Resolving
            # each entry and keeping only the ones still under the root is the
            # check that does not depend on those semantics.
            dirs[:] = sorted(name for name in dirs if _inside(root_resolved, Path(current, name)))
            for name in sorted(files):
                item = Path(current, name)
                if item.is_symlink() or not item.is_file():
                    continue
                if not _inside(root_resolved, item):
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
        # FF-R1 P2: the identical-call guard's streak (B4) rides this
        # channel too, in the same shape the checkpoint seed carries —
        # one mechanism, two carriers; the react loop seeds from
        # whichever resume state is attached.
        "identical_call_state": getattr(task, "_identical_call_state", None),
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
