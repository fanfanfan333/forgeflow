"""INC46 T11 — bundle 导出：``SkillBundle`` → 磁盘目录 / 确定性 zip / JSON。

Where this fits
---------------
:mod:`forgeflow.skills.skill_md` **物化**一份 :class:`SkillBundle`（只算内容，
不落盘）。本模块把该 bundle **导出**成三种便携形态：

* :func:`write_bundle` —— 落盘为 ``<dest_root>/<slug>/...`` 的文件树；
* :func:`bundle_zip_bytes` —— **确定性 zip 字节**（固定 ``date_time``，两次调用
  逐字节一致），可直接作为 HTTP ``application/zip`` 响应体；
* :func:`bundle_to_dict` —— JSON 友好字典，供 API 返回。

:func:`export_skill_bundle` 是从 DB（**权威源**）到 bundle 的唯一入口：**每次
调用都重新读仓储，绝不缓存** —— 因此改了 DB 内容而重导，导出必然随之变化
（反事实：DB 是权威源，不存在本地回填）。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

from forgeflow.repositories import get_skill_repository
from forgeflow.skills.skill_md import SkillBundle, materialize_skill

__all__ = [
    "SkillNotFoundError",
    "export_skill_bundle",
    "write_bundle",
    "bundle_zip_bytes",
    "bundle_to_dict",
]

#: 固定 zip 时间戳（1980-01-01 00:00:00，zip 纪元起点）。**不使用当前时间** ——
#: 否则同一 bundle 两次导出的字节不同（阴性：确定性被破坏）。
_FIXED_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


class SkillNotFoundError(Exception):
    """The requested skill (or its current version) does not exist.

    Raised by :func:`export_skill_bundle`; the API router maps it to HTTP 404
    (诚实 404 —— 绝不返回空壳 bundle / 空 SKILL.md).
    """


async def export_skill_bundle(
    skill_id: str,
    tenant: str,
    *,
    repo: Any | None = None,
) -> SkillBundle:
    """Read skill + its current version from the repository and materialise it.

    **每次调用都重新读仓储**（``get_skill`` / ``get_version``），不做任何缓存：
    DB 是唯一权威源，导出只是物化产物。

    Raises:
        SkillNotFoundError: the skill does not exist for ``tenant``, or the skill
            has no ``current_version``, or that version row is missing. All three
            are the same honest 404 at the API boundary — never a fabricated
            empty bundle.
    """
    repository = repo if repo is not None else get_skill_repository()

    skill = await repository.get_skill(tenant, skill_id)
    if skill is None:
        raise SkillNotFoundError(
            f"skill {skill_id!r} 不存在（tenant={tenant!r}）"
        )

    current = getattr(skill, "current_version", None)
    if not current:
        raise SkillNotFoundError(
            f"skill {skill_id!r} 没有 current_version，无可物化的版本"
        )

    version = await repository.get_version(tenant, skill_id, current)
    if version is None:
        raise SkillNotFoundError(
            f"skill {skill_id!r} 的版本 {current!r} 不存在（tenant={tenant!r}）"
        )

    return materialize_skill(skill, version)


def write_bundle(bundle: SkillBundle, dest_root: str | Path) -> Path:
    """Write the bundle to ``<dest_root>/<slug>/`` and return that skill directory.

    Every parent directory is created on demand; file content is UTF-8.
    """
    root = Path(dest_root) / bundle.slug
    for rel, content in bundle.files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    return root


def bundle_zip_bytes(bundle: SkillBundle) -> bytes:
    """Serialise the bundle into **deterministic** zip bytes.

    A fixed ``date_time`` on every :class:`zipfile.ZipInfo` (never the current
    time) and a sorted file order make two calls on the same bundle produce
    byte-identical output (阳性探针：两次字节相同).
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in sorted(bundle.files):
            info = zipfile.ZipInfo(filename=rel, date_time=_FIXED_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, str(bundle.files[rel]).encode("utf-8"))
    return buffer.getvalue()


def bundle_to_dict(bundle: SkillBundle) -> dict[str, Any]:
    """Project the bundle into a JSON-serialisable dict for the API."""
    return {
        "slug": bundle.slug,
        "skill_id": bundle.skill_id,
        "version": bundle.version,
        "files": dict(bundle.files),
        "content_hash": bundle.content_hash,
        "validation": dict(bundle.validation),
        "skills_ref": dict(bundle.skills_ref),
        "not_materialised": list(bundle.not_materialised),
        "warnings": list(bundle.warnings),
    }
