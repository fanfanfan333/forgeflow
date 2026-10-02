"""Resource Center service (INC25 W1) — register / list / get / preview / resolve.

The service is the single write path for the five resource kinds. It:

  * validates the type BEFORE any byte is persisted (so a rejected upload never
    yields a "已解析" entry — AC-7) and enforces the single-file ceiling via
    :class:`~forgeflow.resources.storage.FileBlobStore` (AC-6);
  * runs the pure summariser (:mod:`forgeflow.resources.summaries`) and persists
    the honest status it returns — ``parsed`` on success, ``metadata_only`` /
    ``ignored`` when an optional dependency is missing (never a fake success);
  * keeps a process-local index of registered records so
    :meth:`ResourceService.resolve_task_inputs` can dereference a declared
    resource id into planning inputs **synchronously** without inventing
    anything (the orchestrator's ``_capability_context`` is a sync seam — U1).

Persistence goes through ``repositories/factory.py::get_resource_repository`` so
the memory and postgres backends behave identically.
"""

from __future__ import annotations

import copy
import hashlib
from typing import Any

from forgeflow.config import get_settings
from forgeflow.repositories.base import utcnow
from forgeflow.repositories.factory import get_resource_repository
from forgeflow.resources import code_sources, summaries
from forgeflow.resources.models import (
    ApiLocator,
    DatabaseLocator,
    FileLocator,
    KbLocator,
    ResourceKind,
    ResourceRecord,
    ResourceSummary,
)
from forgeflow.resources.storage import (
    FileBlobStore,
    ResourceTooLargeError,
    UnsupportedResourceTypeError,
)

__all__ = [
    "ResourceService",
    "get_resource_service",
    "reset_resource_index",
    "ResourceTooLargeError",
    "UnsupportedResourceTypeError",
]

#: Process-local index of registered records, keyed by resource id. Kept so the
#: synchronous dereference seam (``resolve_task_inputs``) can see what this
#: process has registered. It is a cache, never a source of truth: a declared id
#: that is not indexed is simply not dereferenced (never invented).
_RESOURCE_INDEX: dict[str, ResourceRecord] = {}


def _index(record: ResourceRecord) -> None:
    _RESOURCE_INDEX[record.id] = copy.deepcopy(record)


def reset_resource_index() -> None:
    """Drop the process-local dereference index. Test helper."""
    _RESOURCE_INDEX.clear()


class ResourceService:
    """Register / query / dereference task resources."""

    def __init__(self, repo: Any | None = None, blobs: FileBlobStore | None = None) -> None:
        self._repo = repo
        self._blobs = blobs

    @property
    def repo(self) -> Any:
        if self._repo is None:
            self._repo = get_resource_repository()
        return self._repo

    @property
    def blobs(self) -> FileBlobStore:
        if self._blobs is None:
            self._blobs = FileBlobStore()
        return self._blobs

    # ---------------------------------------------------------------- #
    # Registration                                                     #
    # ---------------------------------------------------------------- #
    async def register_file(
        self,
        tenant_id: str | None,
        *,
        name: str,
        data: bytes,
        created_by: str = "",
    ) -> ResourceRecord:
        """Register an uploaded file and persist its real summary.

        Raises:
            UnsupportedResourceTypeError: the file type has no summary path
                (client error → HTTP 400); **no** record is created.
            ResourceTooLargeError: the file exceeds ``multimodal_max_bytes``
                (client error → HTTP 413); **no** record is created.
        """
        filename = (name or "").strip() or "upload"
        if not summaries.is_supported_file(filename):
            dot = filename.rfind(".")
            ext = filename[dot:] if dot >= 0 else "(无扩展名)"
            raise UnsupportedResourceTypeError(
                f"不支持的文件类型：{ext}；支持：{', '.join(summaries.SUPPORTED_FILE_EXTENSIONS)}"
            )
        raw = bytes(data or b"")
        # Size is checked inside put() — it raises ResourceTooLargeError with the
        # actual + limit byte counts before anything is written.
        storage_ref = self.blobs.put(filename, raw)
        status, summary, detail = summaries.summarize_bytes(raw, filename=filename)
        locator = FileLocator(
            filename=filename,
            bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            mime=summaries.mime_for(filename),
            storage_ref=storage_ref,
        )
        record = ResourceRecord(
            tenant_id=tenant_id,
            kind=ResourceKind.FILE.value,
            name=filename,
            created_by=created_by,
            created_at=utcnow().isoformat(),
            status=status,
            detail=detail,
            summary=summary,
            locator=locator,
        )
        return await self._persist(record)

    async def register_database(
        self,
        tenant_id: str | None,
        *,
        table: str,
        created_by: str = "",
    ) -> ResourceRecord:
        """Register a database table.

        The offline (memory) profile has **no** real warehouse, so the summary is
        annotated as a development stub and carries no rows/fields — the platform
        never fabricates table content (Q2 / T01-5).
        """
        table_name = (table or "").strip()
        if not table_name:
            raise ValueError("table 不能为空")
        backend = get_settings().storage_backend.lower()
        if backend == "memory":
            summary = ResourceSummary(
                kind=ResourceKind.DATABASE.value,
                stub=True,
                note="离线档无真实数仓（development stub）",
            )
            detail = "离线档无真实数仓（development stub）"
        else:
            summary = ResourceSummary(
                kind=ResourceKind.DATABASE.value,
                stub=False,
                note="已登记数据库表；资源摘要不读取数据行",
            )
            detail = ""
        locator = DatabaseLocator(table=table_name, backend_hint=backend)
        record = ResourceRecord(
            tenant_id=tenant_id,
            kind=ResourceKind.DATABASE.value,
            name=table_name,
            created_by=created_by,
            created_at=utcnow().isoformat(),
            status="registered",
            detail=detail,
            summary=summary,
            locator=locator,
        )
        return await self._persist(record)

    async def register_code(
        self,
        tenant_id: str | None,
        *,
        source: dict[str, Any],
        created_by: str = "",
    ) -> ResourceRecord:
        """Register a code source (GitHub/GitLab repo, local path, or ZIP)."""
        status, locator, detail = code_sources.register_code_source(dict(source or {}))
        name = locator.identifier or locator.source_type or "code-source"
        summary = ResourceSummary(
            kind=ResourceKind.GIT_REPO.value,
            note=(
                f"{locator.source_type}｜{locator.files_count} 个代码文件｜"
                f"{'、'.join(locator.languages) if locator.languages else '未识别语言'}"
            )
            if locator.reachable
            else "代码来源不可用，未读取到文件统计",
            stub=False,
        )
        record = ResourceRecord(
            tenant_id=tenant_id,
            kind=ResourceKind.GIT_REPO.value,
            name=name,
            created_by=created_by,
            created_at=utcnow().isoformat(),
            status=status,
            detail=detail,
            summary=summary,
            locator=locator,
        )
        return await self._persist(record)

    async def register_knowledge_base(
        self,
        tenant_id: str | None,
        *,
        kb_id: str,
        scope: str = "",
        created_by: str = "",
    ) -> ResourceRecord:
        """Register a knowledge-base resource (登记其可用范围与连通状态）。"""
        kb = (kb_id or "").strip()
        if not kb:
            raise ValueError("kb_id 不能为空")
        locator = KbLocator(
            kb_id=kb,
            scope=(scope or "").strip(),
            reachable=False,
            detail="未提供知识库连通性探测；登记即声明本次任务的可用范围",
        )
        summary = ResourceSummary(kind=ResourceKind.KNOWLEDGE_BASE.value, note=locator.detail)
        record = ResourceRecord(
            tenant_id=tenant_id,
            kind=ResourceKind.KNOWLEDGE_BASE.value,
            name=kb,
            created_by=created_by,
            created_at=utcnow().isoformat(),
            status="registered",
            detail=locator.detail,
            summary=summary,
            locator=locator,
        )
        return await self._persist(record)

    async def register_api(
        self,
        tenant_id: str | None,
        *,
        connector: str,
        base_url: str = "",
        created_by: str = "",
    ) -> ResourceRecord:
        """Register an API connector resource."""
        name = (connector or "").strip()
        if not name:
            raise ValueError("connector 不能为空")
        locator = ApiLocator(
            connector=name,
            base_url=(base_url or "").strip(),
            reachable=False,
            detail="未提供连接器连通性探测；登记即声明本次任务可用的接口",
        )
        summary = ResourceSummary(kind=ResourceKind.API.value, note=locator.detail)
        record = ResourceRecord(
            tenant_id=tenant_id,
            kind=ResourceKind.API.value,
            name=name,
            created_by=created_by,
            created_at=utcnow().isoformat(),
            status="registered",
            detail=locator.detail,
            summary=summary,
            locator=locator,
        )
        return await self._persist(record)

    # ---------------------------------------------------------------- #
    # Query                                                            #
    # ---------------------------------------------------------------- #
    def limits(self) -> dict[str, Any]:
        """Upload limits + supported type whitelist (INC26 T01 / P0-2).

        Both values are read **live** from their single source of truth on every
        call — never copied into a literal here:

          * ``max_bytes`` ← ``Settings.multimodal_max_bytes`` (via
            :func:`forgeflow.config.get_settings`);
          * ``supported_extensions`` ← ``summaries.SUPPORTED_FILE_EXTENSIONS``.

        This is a pure read (no I/O, no network); the returned ``note`` states the
        provenance so a consumer (the front-end pre-check) never hard-codes it.
        """
        return {
            "max_bytes": get_settings().multimodal_max_bytes,
            "supported_extensions": list(summaries.SUPPORTED_FILE_EXTENSIONS),
            "note": "上限与类型白名单来自后端单一事实源，前端不得写死",
        }

    async def list(
        self,
        tenant_id: str | None,
        *,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ResourceRecord]:
        rows = await self.repo.list(tenant_id, kind=kind, limit=limit, offset=offset)
        for row in rows:
            _index(row)
        return rows

    async def get(self, tenant_id: str | None, resource_id: str) -> ResourceRecord | None:
        record = await self.repo.get(tenant_id, resource_id)
        if record is not None:
            _index(record)
        return record

    async def delete(self, tenant_id: str | None, resource_id: str) -> bool:
        """Delete one registered resource (tenant-scoped). Returns True iff a row went.

        Semantics:
          * Removes the record from the repository **and** drops it from the
            process-local dereference index ``_RESOURCE_INDEX`` in the same step,
            so :meth:`resolve_task_inputs` can no longer dereference a deleted
            resource (no dangling reference — INC40 / B6);
          * **does not delete the underlying blob** — ``FileBlobStore`` is
            content-addressed and de-duplicates identical bytes onto one path, so
            a blind delete could break another resource's preview;
          * the repository ``delete`` is itself idempotent (repeat = no-op).
        """
        record = await self.repo.get(tenant_id, resource_id)
        if record is None:
            return False
        await self.repo.delete(tenant_id, resource_id)
        _RESOURCE_INDEX.pop(resource_id, None)
        return True

    async def preview(self, tenant_id: str | None, resource_id: str, n: int = 20) -> dict[str, Any]:
        """Preview a resource's content (first ``n`` rows / lines).

        Honest empty state for anything not directly previewable (database / code
        / KB / API, or a file whose bytes are gone): ``available=False`` + a note,
        never invented content.
        """
        record = await self.get(tenant_id, resource_id)
        if record is None:
            return {"id": resource_id, "kind": "", "available": False, "format": "none",
                    "columns": [], "rows": [], "content": "", "truncated": False,
                    "note": "资源不存在"}
        limit = max(1, int(n))
        if record.kind != ResourceKind.FILE.value:
            return {"id": record.id, "kind": str(record.kind), "available": False,
                    "format": "none", "columns": [], "rows": [], "content": "",
                    "truncated": False, "note": f"{record.kind} 资源不支持内容预览"}
        storage_ref = ""
        loc = record.locator
        storage_ref = loc.storage_ref if isinstance(loc, FileLocator) else str(
            (loc or {}).get("storage_ref", "") if isinstance(loc, dict) else ""
        )
        if not storage_ref or not self.blobs.exists(storage_ref):
            return {"id": record.id, "kind": "file", "available": False, "format": "none",
                    "columns": [], "rows": [], "content": "", "truncated": False,
                    "note": "文件内容不可读取"}
        text, _encoding = summaries._decode(self.blobs.read(storage_ref))
        content_kind = summaries.content_kind(record.name)
        # INC43 T04-fix — a ``.docx`` is not a line-previewable text file: report
        # the honest empty state (its real content is exposed through the document
        # plane, not the row/line preview) rather than decoding binary garbage.
        if content_kind == "document":
            return {"id": record.id, "kind": "file", "available": False, "format": "document",
                    "columns": [], "rows": [], "content": "", "truncated": False,
                    "note": "DOCX 文档不支持文本行预览；请使用文档编辑能力读取或修改"}
        if content_kind == "table":
            import csv as _csv
            import io as _io

            delim = summaries._sniff_delimiter(text, record.name)
            parsed = list(_csv.reader(_io.StringIO(text), delimiter=delim))
            header = parsed[0] if parsed else []
            body = parsed[1:]
            truncated = len(body) > limit
            return {"id": record.id, "kind": "file", "available": True, "format": "table",
                    "columns": [str(c) for c in header],
                    "rows": [[str(c) for c in row] for row in body[:limit]],
                    "content": "", "truncated": truncated,
                    "note": f"仅预览前 {limit} 行" if truncated else ""}
        lines = text.splitlines()
        truncated = len(lines) > limit
        return {"id": record.id, "kind": "file", "available": True, "format": "text",
                "columns": [], "rows": [], "content": "\n".join(lines[:limit]),
                "truncated": truncated,
                "note": f"仅预览前 {limit} 行" if truncated else ""}

    # ---------------------------------------------------------------- #
    # Dereference (declaration → planning inputs)                      #
    # ---------------------------------------------------------------- #
    def resolve_task_inputs(
        self,
        context: dict[str, Any],
        *,
        records: list[ResourceRecord] | None = None,
    ) -> dict[str, Any]:
        """Dereference declared resource ids into planning inputs (U1).

        Pure dereference: it reads ONLY the registered attributes of the resources
        the caller explicitly declared under ``context['resources']`` and maps them
        onto the planner's real input keys (``table`` / ``paths`` / ``repo_path``,
        plus ``document_paths`` / ``document_names`` for a registered ``.docx``
        FILE — INC43 T04-fix). It never invents a table name or a path, and returns
        ``{}`` when nothing was declared. The result is meant to feed
        ``CapabilityContext`` — it is **never** written back to ``task.context``.

        ``records`` (when supplied by an async caller that already loaded them) is
        preferred; otherwise the process-local index is consulted.

        ``document_paths`` is **additive**: ``paths`` still receives every
        dereferenced FILE path exactly as before, so the code/analysis seams are
        byte-for-byte unchanged; a ``.docx`` additionally appears here so the
        document plane (:func:`_is_document_task`) can key on a real document
        signal rather than a forward-compatible guess.

        ``document_names`` is the **index-aligned** companion of
        ``document_paths`` (same loop, same guard ⇒ ``document_names[i]`` is the
        registered ``ResourceRecord.name`` for ``document_paths[i]``). It exists so
        the produced deliverable can carry the user's **real** file name instead of
        the content-addressed path's hash; it is additive and only ever holds a real
        registered name (never an invented one).
        """
        ids = context.get("resources") if isinstance(context, dict) else None
        if not isinstance(ids, (list, tuple)) or not ids:
            return {}
        by_id: dict[str, ResourceRecord] = {}
        if records:
            by_id = {r.id: r for r in records}
        resolved: dict[str, Any] = {}
        paths: list[str] = []
        document_paths: list[str] = []
        document_names: list[str] = []
        repo_path = ""
        table = ""
        for rid in ids:
            record = by_id.get(str(rid)) or _RESOURCE_INDEX.get(str(rid))
            if record is None:
                continue
            kind = str(record.kind)
            loc = record.locator
            if kind == ResourceKind.GIT_REPO.value:
                ident = str(getattr(loc, "identifier", "") or "").strip()
                if ident:
                    repo_path = repo_path or ident
                    if ident not in paths:
                        paths.append(ident)
            elif kind == ResourceKind.DATABASE.value:
                name = str(getattr(loc, "table", "") or "").strip()
                if name and not table:
                    table = name
            elif kind == ResourceKind.FILE.value:
                storage_ref = str(getattr(loc, "storage_ref", "") or "").strip()
                if storage_ref:
                    try:
                        file_path = str(self.blobs.resolve(storage_ref))
                    except Exception:  # noqa: BLE001 — a bad ref simply contributes nothing
                        file_path = ""
                    if file_path and file_path not in paths:
                        paths.append(file_path)
                    # INC43 T04-fix — a registered ``.docx`` additionally feeds the
                    # document plane. ``paths`` above is unchanged (the code /
                    # analysis seams keep seeing every FILE path verbatim). The
                    # name is appended in the SAME guard as the path so the two
                    # lists stay index-aligned (a duplicate path is skipped for
                    # both, never for one only).
                    if (
                        file_path
                        and summaries.content_kind(record.name) == "document"
                        and file_path not in document_paths
                    ):
                        document_paths.append(file_path)
                        document_names.append(str(record.name or "").strip())
        if table:
            resolved["table"] = table
        if paths:
            resolved["paths"] = paths
        if repo_path:
            resolved["repo_path"] = repo_path
        if document_paths:
            resolved["document_paths"] = document_paths
            resolved["document_names"] = document_names
        return resolved

    # ---------------------------------------------------------------- #
    # Internal                                                         #
    # ---------------------------------------------------------------- #
    async def _persist(self, record: ResourceRecord) -> ResourceRecord:
        saved = await self.repo.save(record)
        _index(saved)
        return saved


def get_resource_service() -> ResourceService:
    """Construct a service bound to the active backend + store root."""
    return ResourceService()
