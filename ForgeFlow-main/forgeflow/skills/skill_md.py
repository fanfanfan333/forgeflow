"""INC46 T11 — SKILL.md 物化：DB（权威源）→ 合规 ``SKILL.md`` + bundle 文件树。

What this module is
-------------------
**PostgreSQL 是权威源；文件只是物化产物。** 本模块把 DB 里某个
:class:`~forgeflow.skills.models.SkillVersionRecord` 的七段契约 ``spec``
**物化**成一份符合官方 Agent Skills 规范的 :class:`SkillBundle`：一份
``SKILL.md``（正文 + frontmatter）以及（仅当契约真的提供了内容时）``references/``
/ ``scripts/`` 下的附属文件。

它**不**自证合规：渲染交给 :func:`forgeflow.skills.spec_mapping.render_skill_md`，
合规性由 :func:`forgeflow.skills.spec_validator.validate_skill_md` 判定；官方
``skills-ref`` 交由 :func:`forgeflow.skills.spec_validator.validate_with_skills_ref`
判定（本机未安装 ⇒ 诚实 skipped，红线 10，绝不算 PASS）。

Failure behaviour（失败表现，红线：禁止静默补全）
------------------------------------------------
* 契约缺必需段 / 字段非法 ⇒ 抛 :class:`SkillMaterializationError`，异常消息含
  **全部** 具体错误（段名 / 字段名 + 原因），**不** 静默补全任何段；
* 契约 manifest 缺 ``name``（ASCII slug，A3：系统生成并锁定）⇒ 抛
  :class:`SkillMaterializationError` —— **不猜测** slug（绝不把中文显示名或
  目录名当 name）；
* 声明的 ``references/`` / ``scripts/`` / ``assets/evals/`` **没有正文内容** ⇒
  **不伪造空文件**，而是记入 :attr:`SkillBundle.not_materialised`。

Materialisation content channel（物化内容通道）
-----------------------------------------------
T07 的七段契约模型对每个段都 ``extra="forbid"``（见
:mod:`forgeflow.skills.schemas`），因此 **正文内容** 无法作为契约字段随身携带。
本模块支持一个**显式的非契约**内容通道，在调用
:func:`validate_contract_document` **之前**被精确剥离（只剥被明确消费的
content 键，其余字段一律照旧严格校验）：

* 顶层保留键 ``_materialization``：``{"references": {file: 正文},
  "scripts": {name: 正文}}``；或
* 内联便捷键：``knowledge.references[i]["content"]`` 与
  ``tool_bindings["script_contents"]``（等价，均被提升到同一通道）。

剥离只针对上述 exact 键 —— 任何**其他**多余字段仍会被 T07 如实拒绝，因此
契约的严格性未被削弱。
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from forgeflow.skills.models import SkillRecord, SkillVersionRecord
from forgeflow.skills.schemas import validate_contract_document
from forgeflow.skills.segments import normalise_segment_key, normalize_reference
from forgeflow.skills.spec_mapping import render_skill_md
from forgeflow.skills.spec_validator import (
    validate_skill_md,
    validate_with_skills_ref,
)

__all__ = [
    "SkillMaterializationError",
    "SkillBundle",
    "compute_content_hash",
    "materialize_skill",
]

#: 顶层保留键：显式的物化内容通道（非契约字段，校验前被剥离）。
_MATERIALIZATION_KEY = "_materialization"


class SkillMaterializationError(Exception):
    """Raised when a version contract cannot be materialised.

    Carries **every** concrete error (segment / field + reason) in
    :attr:`errors`; the message is the joined list. It is raised — never
    swallowed — so a malformed contract is reported honestly instead of being
    silently completed (禁止静默补全).
    """

    def __init__(self, errors: list[str] | tuple[str, ...] | str) -> None:
        if isinstance(errors, str):
            errors = [errors]
        self.errors: list[str] = [str(e) for e in errors]
        super().__init__("；".join(self.errors) or "skill 物化失败")


@dataclass
class SkillBundle:
    """A materialised skill: the file tree + its honest validation record.

    ``files`` maps **relative POSIX paths** → text content and always contains
    ``SKILL.md``. ``content_hash`` is a sha256 over the sorted ``(path, content)``
    list, so it is **re-computable** by any third party. ``validation`` is the
    :meth:`SkillValidationReport.to_dict` of the rendered ``SKILL.md``;
    ``skills_ref`` is the (possibly honestly *skipped*) official-tool result.
    """

    slug: str
    skill_id: str
    version: str
    files: dict[str, str]
    content_hash: str
    validation: dict[str, Any]
    skills_ref: dict[str, Any]
    not_materialised: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def compute_content_hash(files: Mapping[str, str]) -> str:
    """sha256 over the sorted ``(path, content)`` list — the re-computable currency.

    Deterministic and order-independent: two bundles with the same file set hash
    identically, and the digest can be reproduced by anyone replaying the same
    files (阳性探针：两次导出内容一致).
    """
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(files[path]).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# contract <-> materialisation-content separation                              #
# --------------------------------------------------------------------------- #
def _find_segment(spec: Any, canonical: str) -> Any:
    """Return the raw value of a segment by canonical key (alias-tolerant)."""
    if not isinstance(spec, Mapping):
        return None
    for key, value in spec.items():
        if normalise_segment_key(key) == canonical:
            return value
    return None


def _extract_slug(spec: Any) -> str:
    """The locked ASCII ``name`` slug from the contract manifest, or ``""``.

    The slug is read from ``manifest.name`` only (A3: system-generated and
    locked). It is **never** derived from the display name or the directory —
    a missing slug is an explicit failure, not a guess.
    """
    manifest = _find_segment(spec, "manifest")
    if not isinstance(manifest, Mapping):
        return ""
    return str(manifest.get("name") or "").strip()


def _split_materialization(spec: Any) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Separate the strict contract from the explicit content channel.

    Returns ``(contract_only_spec, sidecar)`` where ``sidecar`` is
    ``{"references": {file: content}, "scripts": {name: content}}``. Only the
    documented content keys are lifted; every other field stays in the contract
    and is still validated strictly by T07 (契约严格性未削弱).
    """
    if not isinstance(spec, Mapping):
        return {}, {"references": {}, "scripts": {}}

    contract: dict[str, Any] = dict(spec)
    sidecar: dict[str, dict[str, str]] = {"references": {}, "scripts": {}}

    # 1) explicit top-level sidecar key (non-contract, always stripped).
    explicit = contract.pop(_MATERIALIZATION_KEY, None)
    if isinstance(explicit, Mapping):
        for bucket in ("references", "scripts"):
            values = explicit.get(bucket)
            if isinstance(values, Mapping):
                for name, content in values.items():
                    if content is not None and str(content) != "":
                        sidecar[bucket][str(name)] = str(content)

    # 2) inline aliases: knowledge.references[i]["content"].
    knowledge = _find_segment(contract, "knowledge")
    if isinstance(knowledge, Mapping) and isinstance(knowledge.get("references"), list):
        cleaned: list[Any] = []
        for item in knowledge["references"]:
            if isinstance(item, Mapping) and "content" in item:
                pulled = dict(item)
                content = pulled.pop("content")
                file = str(item.get("file") or "").strip()
                if file and content is not None and str(content) != "":
                    sidecar["references"][file] = str(content)
                cleaned.append(pulled)
            else:
                cleaned.append(item)
        new_knowledge = dict(knowledge)
        new_knowledge["references"] = cleaned
        _replace_segment(contract, "knowledge", new_knowledge)

    # 3) inline alias: tool_bindings["script_contents"].
    bindings = _find_segment(contract, "tool_bindings")
    if isinstance(bindings, Mapping) and isinstance(bindings.get("script_contents"), Mapping):
        new_bindings = dict(bindings)
        contents = new_bindings.pop("script_contents")
        for name, content in dict(contents).items():
            if content is not None and str(content) != "":
                sidecar["scripts"][str(name)] = str(content)
        _replace_segment(contract, "tool_bindings", new_bindings)

    return contract, sidecar


def _replace_segment(spec: dict[str, Any], canonical: str, value: Any) -> None:
    """Replace a segment's value in place, preserving its original key spelling."""
    for key in list(spec.keys()):
        if normalise_segment_key(key) == canonical:
            spec[key] = value
            return


# --------------------------------------------------------------------------- #
# the materialiser                                                             #
# --------------------------------------------------------------------------- #
def materialize_skill(skill: SkillRecord, version: SkillVersionRecord) -> SkillBundle:
    """Materialise ``version`` into a spec-compliant :class:`SkillBundle`.

    Pipeline: split off the explicit content channel → ``validate_contract_document``
    (fail ⇒ :class:`SkillMaterializationError` with every error) →
    ``document.to_contract_dict()`` → ``render_skill_md`` → ``validate_skill_md``
    → attach ``references/`` / ``scripts/`` bodies **only when the contract
    supplied content**, else record them in ``not_materialised``.

    Raises:
        SkillMaterializationError: the contract has no manifest ``name`` slug, or
            fails T07 validation, or its rendered ``SKILL.md`` is itself invalid.
    """
    spec = getattr(version, "spec", None)
    if not isinstance(spec, Mapping):
        raise SkillMaterializationError(
            f"版本 {getattr(version, 'semver', '')!r} 的 spec 不是键值映射，无法物化"
        )

    slug = _extract_slug(spec)
    if not slug:
        raise SkillMaterializationError(
            "契约 manifest 缺少 name（ASCII slug）：不得猜测 slug —— 请显式提供 "
            "manifest.name（A3：slug 系统生成并锁定，改名不变目录名）"
        )

    contract_only, sidecar = _split_materialization(spec)

    report = validate_contract_document(contract_only, parent_dir=slug)
    if not report.ok or report.document is None:
        raise SkillMaterializationError(report.errors or ["契约校验失败"])
    document = report.document

    skill_md_text = render_skill_md(document.to_contract_dict())
    validation = validate_skill_md(skill_md_text, parent_dir=slug)
    if not validation.ok:
        raise SkillMaterializationError(
            ["渲染出的 SKILL.md 未通过规范校验：" + "；".join(validation.errors)]
        )

    files: dict[str, str] = {"SKILL.md": skill_md_text}
    not_materialised: list[str] = []

    # references/ — 只有契约给了正文内容才落盘，否则如实登记（不伪造空文件）。
    if document.knowledge is not None:
        for ref in document.knowledge.references:
            path = normalize_reference(ref.file, default_prefix="references/")
            content = (
                sidecar["references"].get(ref.file)
                or sidecar["references"].get(path)
                or ""
            )
            if str(content).strip():
                files[path] = str(content)
            else:
                not_materialised.append(
                    f"{path}（契约仅声明引用、未提供正文内容；不伪造空文件）"
                )

    # scripts/ — 同上：有内容才落盘，否则只做声明。
    if document.tool_bindings is not None:
        for script in document.tool_bindings.scripts:
            path = normalize_reference(script, default_prefix="scripts/")
            content = (
                sidecar["scripts"].get(script) or sidecar["scripts"].get(path) or ""
            )
            if str(content).strip():
                files[path] = str(content)
            else:
                not_materialised.append(
                    f"{path}（契约仅声明脚本、未提供脚本内容；不伪造空文件）"
                )

    # assets/evals/ — 评估集只声明引用；权威记录始终在 ForgeFlow DB。
    if document.evaluation is not None:
        ref = str(document.evaluation.ref or "").strip()
        if ref:
            not_materialised.append(
                f"{ref}（评估集仅声明引用、未提供内容；权威评估记录在 ForgeFlow DB）"
            )

    skills_ref = _run_skills_ref(slug, files)

    warnings: list[str] = []
    if skills_ref.get("status") == "skipped":
        warnings.append(
            "官方 skills-ref 校验未执行（status=skipped）：本次未取得官方 PASS，"
            f"**不等于通过**。原因：{skills_ref.get('reason')}"
        )

    return SkillBundle(
        slug=slug,
        skill_id=str(getattr(skill, "id", "") or ""),
        version=str(getattr(version, "semver", "") or ""),
        files=files,
        content_hash=compute_content_hash(files),
        validation=validation.to_dict(),
        skills_ref=skills_ref,
        not_materialised=not_materialised,
        warnings=warnings,
    )


def _run_skills_ref(slug: str, files: Mapping[str, str]) -> dict[str, Any]:
    """Materialise ``files`` into a temp dir and run the official validator.

    The bundle is written to a throwaway directory so the official tool (when
    present) is invoked on the real tree — the day ``skills-ref`` exists this
    runs for real; while it is absent the result is honestly *skipped*.
    """
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / slug
        for rel, content in files.items():
            target = skill_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return validate_with_skills_ref(str(skill_dir))
