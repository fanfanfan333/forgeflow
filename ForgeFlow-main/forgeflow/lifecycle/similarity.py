"""INC46 T35 — 去重 / 合并提案（description 余弦 + 工具序列 Jaccard）。

任务书 T35 §规格
================
* **触发**：``描述嵌入余弦 ≥ 0.85`` **且** ``工具序列 Jaccard ≥ 0.7``（初始默认）
  ⇒ 生成 **merge proposal**，**不自动合并**。
* 合并只产生 **新 Skill / 版本**并保留**来源链**（``source_skill_ids`` 含两个源 ID）。
* 阴性探针：**描述相似但工具序列不同 ⇒ 不误合并** —— 故两个阈值必须**同时**满足。

实现口径
--------
* **描述余弦**复用 T09 的稠密分支 :func:`forgeflow.skills.retrieval.dense_scores`
  （同一套逐字预处理 —— 中文若不预处理，稠密余弦恒为 0，会静默失效，见该模块
  裁定 V-3 的长注释）。算不出来（``None``）⇒ **不判为近重复**（往「不合并」一侧
  fail-closed，绝不因为「测不出」而误合并）。
* **工具 Jaccard** = ``|A ∩ B| / |A ∪ B|``，A / B 为两 skill 声明的**工具集合**
  （“序列”在这里按声明工具集理解：顺序不改变「能否覆盖同一批工具」这一语义，
  且能正确拒绝「工具不同」的近描述对）。两边都为空 ⇒ Jaccard 视为 ``0.0``
  （没有证据支持合并，不得因为「都空」而判相似）。
* **提案不自动合并**：:func:`propose_merges` 只写提案行；合并动作在人工
  approve 后由 :func:`plan_merged_skill` 产出的**新** skill / 版本承载（红线 6：
  不覆盖历史版本）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from forgeflow.skills.retrieval import dense_scores

if TYPE_CHECKING:  # pragma: no cover - typing only
    from forgeflow.lifecycle.store import LifecycleStore, MergeProposalRecord

__all__ = [
    "MERGE_COSINE_THRESHOLD",
    "MERGE_JACCARD_THRESHOLD",
    "MergeCandidate",
    "tool_ids",
    "tool_sequence_jaccard",
    "description_cosine",
    "find_merge_candidates",
    "propose_merges",
    "plan_merged_skill",
    "approve_proposal",
]

#: 初始默认阈值（§十：余弦 ≥ 0.85 且 Jaccard ≥ 0.7；T36 人工审核通过率校准后再定）。
MERGE_COSINE_THRESHOLD: float = 0.85
MERGE_JACCARD_THRESHOLD: float = 0.7


@dataclass(frozen=True)
class MergeCandidate:
    """一对近重复 skill（两个阈值**同时**满足）。"""

    primary_skill_id: str
    duplicate_skill_id: str
    description_cosine: float
    tool_jaccard: float

    @property
    def source_skill_ids(self) -> list[str]:
        return sorted({self.primary_skill_id, self.duplicate_skill_id})

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_skill_id": self.primary_skill_id,
            "duplicate_skill_id": self.duplicate_skill_id,
            "description_cosine": self.description_cosine,
            "tool_jaccard": self.tool_jaccard,
            "source_skill_ids": self.source_skill_ids,
        }


def _field(skill: Any, name: str) -> str:
    return str(getattr(skill, name, "") or "")


def _declared_tools(spec: Mapping[str, Any] | None) -> list[str]:
    if not spec:
        return []
    tools = spec.get("tools") or []
    return [str(t).strip() for t in tools if str(t).strip()]


def tool_ids(skill: Any, specs: Mapping[str, Mapping[str, Any]] | None = None) -> list[str]:
    """该 skill 声明的工具集合。

    优先取 ``specs[skill_id]["tools"]``（当前版本的 spec），否则回退到
    ``skill.tools``（若对象直接带该字段）。两者皆无 ⇒ 空列表。
    """
    skill_id = _field(skill, "id")
    if specs and skill_id in specs:
        return _declared_tools(specs.get(skill_id))
    direct = getattr(skill, "tools", None)
    if direct:
        return [str(t).strip() for t in direct if str(t).strip()]
    spec = getattr(skill, "spec", None)
    return _declared_tools(spec if isinstance(spec, Mapping) else None)


def tool_sequence_jaccard(tools_a: Sequence[str] | None, tools_b: Sequence[str] | None) -> float:
    """两批工具集合的 Jaccard 相似度 ∈ [0, 1]；任一为空 ⇒ ``0.0``。"""
    a = {str(t).strip() for t in (tools_a or ()) if str(t).strip()}
    b = {str(t).strip() for t in (tools_b or ()) if str(t).strip()}
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def description_cosine(left: str, right: str) -> float | None:
    """两段描述的稠密余弦（真实计算）；算不出来 ⇒ ``None``（未测量）。"""
    try:
        value = dense_scores(left or "", [right or ""])[0]
    except Exception:  # noqa: BLE001 — embedding 不可用时不得误判为近重复
        return None
    return value


def find_merge_candidates(
    skills: Sequence[Any],
    *,
    specs: Mapping[str, Mapping[str, Any]] | None = None,
    cosine_threshold: float = MERGE_COSINE_THRESHOLD,
    jaccard_threshold: float = MERGE_JACCARD_THRESHOLD,
) -> list[MergeCandidate]:
    """找出所有**同时**满足余弦与 Jaccard 阈值的近重复对（确定性顺序）。

    只比较不同 id 的 skill；结果按 ``(primary_id, duplicate_id)`` 字典序排序，
    同一次输入恒定产出同一批候选（可回放）。
    """
    by_id = {_field(s, "id"): s for s in skills if _field(s, "id")}
    ids = sorted(by_id)
    out: list[MergeCandidate] = []
    for i, left_id in enumerate(ids):
        for right_id in ids[i + 1 :]:
            left, right = by_id[left_id], by_id[right_id]
            cos = description_cosine(_field(left, "description"), _field(right, "description"))
            if cos is None or cos < cosine_threshold:
                continue
            jac = tool_sequence_jaccard(
                tool_ids(left, specs), tool_ids(right, specs)
            )
            if jac < jaccard_threshold:
                continue
            out.append(
                MergeCandidate(
                    primary_skill_id=left_id,
                    duplicate_skill_id=right_id,
                    description_cosine=cos,
                    tool_jaccard=jac,
                )
            )
    return out


def propose_merges(
    store: "LifecycleStore",
    tenant_id: str | None,
    skills: Sequence[Any],
    *,
    specs: Mapping[str, Mapping[str, Any]] | None = None,
    cosine_threshold: float = MERGE_COSINE_THRESHOLD,
    jaccard_threshold: float = MERGE_JACCARD_THRESHOLD,
) -> list["MergeProposalRecord"]:
    """为每个近重复对**生成提案**（不自动合并；同一对幂等，不重复建）。

    Raises:
        LifecycleStoreError: 租户未解析（由 store 的 fail-closed 抛出）。
    """
    from forgeflow.lifecycle.store import MergeProposalRecord

    proposals: list[MergeProposalRecord] = []
    for cand in find_merge_candidates(
        skills,
        specs=specs,
        cosine_threshold=cosine_threshold,
        jaccard_threshold=jaccard_threshold,
    ):
        existing = store.find_proposal(
            tenant_id, cand.primary_skill_id, cand.duplicate_skill_id
        )
        if existing is not None:
            proposals.append(existing)
            continue
        record = MergeProposalRecord(
            tenant_id=str(tenant_id or ""),
            primary_skill_id=cand.primary_skill_id,
            duplicate_skill_id=cand.duplicate_skill_id,
            source_skill_ids=cand.source_skill_ids,
            description_cosine=cand.description_cosine,
            tool_jaccard=cand.tool_jaccard,
        )
        store.save_proposal(record)
        proposals.append(record)
    return proposals


def plan_merged_skill(
    proposal: "MergeProposalRecord",
    primary_skill: Any,
    duplicate_skill: Any,
) -> dict[str, Any]:
    """产出「合并后的新 skill + 新版本」的**计划**（纯函数，不落库）。

    * 新 skill 继承 primary 的 name / domain / description，标签并集；
    * 新版本 spec 显式携带 ``merged_from = source_skill_ids`` —— 这就是**来源链**，
      使合并结果可追溯回两个源 skill；
    * **不删除**任何源 skill 或其版本（红线 6）。
    """
    tags = sorted(
        {str(t) for t in (_field_list(primary_skill, "tags") + _field_list(duplicate_skill, "tags"))}
    )
    sources = list(proposal.source_skill_ids) or sorted(
        {_field(primary_skill, "id"), _field(duplicate_skill, "id")} - {""}
    )
    return {
        "skill": {
            "name": _field(primary_skill, "name"),
            "domain": _field(primary_skill, "domain"),
            "description": _field(primary_skill, "description"),
            "tags": tags,
            "merged_from": sources,
        },
        "version": {
            "semver": "1.0.0",
            "spec": {
                "merged_from": sources,
                "prompt": f"合并技能：{_field(primary_skill, 'name')}",
                "steps": [],
                "tools": [],
                "io_schema": {"input": {}, "output": {}},
                "applicable_when": {},
            },
            "changelog": "由 T35 去重提案合并生成（来源链：" + ", ".join(sources) + "）",
        },
        "source_skill_ids": sources,
    }


def _field_list(obj: Any, name: str) -> list[str]:
    value = getattr(obj, name, None) or []
    try:
        return [str(v) for v in value]
    except TypeError:
        return []


def approve_proposal(
    store: "LifecycleStore",
    tenant_id: str | None,
    proposal_id: str,
    *,
    actor: str | None = None,
    merged_skill_id: str | None = None,
    now: datetime | None = None,
) -> "MergeProposalRecord":
    """人工 approve 一条提案（记录决策者 / 时间 / 合并产物）。

    Raises:
        LifecycleError: 提案不存在或已被决策（fail-closed：不重复 approve）。
    """
    from forgeflow.lifecycle.state_machine import LifecycleError
    from forgeflow.lifecycle.store import PROPOSAL_APPROVED, PROPOSAL_PROPOSED

    if not tenant_id:
        raise LifecycleError("拒绝审批：未解析租户（fail-closed，红线 5）")
    proposal = store.get_proposal(tenant_id, proposal_id)
    if proposal is None:
        raise LifecycleError(f"提案不存在：{proposal_id}")
    if proposal.status != PROPOSAL_PROPOSED:
        raise LifecycleError(f"提案已被决策：{proposal_id}（status={proposal.status}）")
    proposal.status = PROPOSAL_APPROVED
    proposal.decided_by = actor
    proposal.decided_at = now
    proposal.merged_skill_id = merged_skill_id
    return store.update_proposal(proposal)
