"""Authoritative, tenant-scoped input admission and per-run resource snapshots."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


class InputReferenceError(ValueError):
    """An input is malformed or unavailable to the requesting tenant."""

    def __init__(self, message: str, status_code: int = 404) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class InputScope:
    tenant_id: str | None
    resources: dict[str, Any]
    parents: list[Any] = field(default_factory=list)
    inherited_inputs: dict[str, Any] = field(default_factory=dict)


current_input_scope: ContextVar[InputScope | None] = ContextVar(
    "forgeflow_input_scope", default=None
)


async def prepare_input_scope(task: Any, ctx: Any) -> InputScope:
    """Load declared inputs from storage; never authorize through a shared cache."""
    from forgeflow.resources.service import get_resource_service

    context = task.context or {}
    session_id = context.get("session_id")
    if session_id and not getattr(task, "_functional_eval", False):
        if not isinstance(session_id, str):
            raise InputReferenceError("Session not found")
        from forgeflow.workspace.store import get_workspace_store

        runs = await get_workspace_store().list_session_runs(ctx.tenant_id, session_id)
        if not runs or any(r.actor_user_id != ctx.user_id for r in runs):
            raise InputReferenceError("Session not found")
    if "instruction_draft" in context:
        from forgeflow.rbac.enforcer import RBACEnforcer

        draft = context["instruction_draft"]
        if not RBACEnforcer().check(ctx.role, "write", "skills"):
            raise InputReferenceError("试运行技能需要技能管理权限", 403)
        if (
            not isinstance(draft, dict)
            or not isinstance(draft.get("instructions"), str)
            or not 10 <= len(draft["instructions"].strip()) <= 12000
        ):
            raise InputReferenceError("技能草案指令无效", 400)
    selected = context.get("skill_ids", [])
    if (
        not isinstance(selected, list)
        or len(selected) > 3
        or any(not isinstance(s, str) or not s.strip() for s in selected)
    ):
        raise InputReferenceError("skill_ids must contain at most three skill IDs", 400)
    if selected:
        from forgeflow.skills.registry import SkillRegistry

        registry = SkillRegistry()
        for skill_id in selected:
            skill = await registry.get(ctx.tenant_id, skill_id)
            if skill is None or skill.status == "archived":
                raise InputReferenceError("Skill not found")
            if skill.status != "published" or not skill.current_version:
                raise InputReferenceError("该技能尚未启用，请先完成发布", 409)
            if not any(
                v.semver == skill.current_version
                for v in await registry.versions(ctx.tenant_id, skill_id)
            ):
                raise InputReferenceError("技能的当前版本不存在，请重新保存指令", 409)
    raw_ids = context.get("resources", [])
    if not isinstance(raw_ids, (list, tuple)) or any(
        not isinstance(rid, str) or not rid.strip() for rid in raw_ids
    ):
        raise InputReferenceError("resources must be a list of non-empty resource IDs", 400)
    ids = list(dict.fromkeys(raw_ids))
    parents = []
    # Both aliases must be checked, even when only one is selected by the planner.
    for key in ("parent_run_id", "continued_from_run_id"):
        parent_id = context.get(key)
        if not parent_id:
            continue
        if not isinstance(parent_id, str):
            raise InputReferenceError("Parent run ID must be a string", 400)
        from forgeflow.runtime.dispatcher import load_run_header

        parent = await load_run_header(ctx.tenant_id, parent_id)
        if (parent is None or parent.tenant_id != ctx.tenant_id
                or getattr(parent, "actor_user_id", None) != ctx.user_id):
            raise InputReferenceError("Parent run not found")
        if not parents:
            current = parent
            visited = set()
            for _ in range(12):
                if current.run_id in visited:
                    break
                visited.add(current.run_id)
                parents.append(current)
                older = getattr(current, "parent_run_id", "") or ""
                if not older:
                    break
                current = await load_run_header(ctx.tenant_id, older)
                if current is None:
                    break  # Deleted history is not resurrected.
                if (current.tenant_id != ctx.tenant_id
                        or getattr(current, "actor_user_id", None) != ctx.user_id):
                    raise InputReferenceError("Parent run not found")
        inherited = (getattr(parent, "declared_inputs", None) or {}).get("resources", [])
        if isinstance(inherited, (list, tuple)):
            ids.extend(rid for rid in inherited if isinstance(rid, str) and rid not in ids)
    service = get_resource_service()
    records = {}
    for rid in ids:
        record = await service.get(ctx.tenant_id, rid)
        if record is None or record.tenant_id != ctx.tenant_id:
            raise InputReferenceError("Resource not found")
        records[rid] = record
    for parent in parents:
        for rid in (getattr(parent, "declared_inputs", None) or {}).get("resources", []):
            if rid not in records and isinstance(rid, str):
                record = await service.get(ctx.tenant_id, rid)
                if record is not None and record.tenant_id == ctx.tenant_id:
                    records[rid] = record
    inherited_inputs = service.resolve_task_inputs(
        {"resources": list(records)}, records=list(records.values())
    )
    # Continue from the most recent delivered file, rather than re-editing the
    # original upload. Only server-owned references from tenant-checked runs.
    if not raw_ids:
        from forgeflow.documents.store import DocArtifactStore

        store = DocArtifactStore()
        for parent in parents:
            artifacts = [a for a in getattr(parent, "artifacts", []) if a.get("content_ref")]
            if not artifacts:
                continue
            for artifact in artifacts:
                fmt = artifact.get("format")
                plane = {
                    "docx": "document",
                    "pptx": "document",
                    "text": "text",
                    "txt": "text",
                    "xlsx": "sheet",
                    "xlsm": "sheet",
                    "pdf": "pdf",
                }.get(fmt)
                if not plane:
                    continue
                ref = artifact["content_ref"]
                try:
                    path = store.resolve(ref)
                except (OSError, ValueError) as exc:
                    raise InputReferenceError("Previous file content unavailable") from exc
                # Cross-project result passing bug: the old code
                # overwrote each plane's list on every artifact, so a
                # parent run with several delivered files only passed
                # the *last* file of each plane to the next project.
                # Accumulate instead; the first parent with artifacts
                # still wins (the ``break`` below), as before.
                inherited_inputs.setdefault(f"{plane}_paths", []).append(str(path))
                inherited_inputs.setdefault(f"{plane}_names", []).append(
                    artifact.get("title") or path.name
                )
                inherited_inputs.setdefault("paths", []).append(str(path))
            break
    return InputScope(ctx.tenant_id, records, parents, inherited_inputs)
