"""INC46 T35 — 跨 Skill 冲突检测 + 检索降权（复用 T08 的冲突检测接口）。

任务书 T35 §规格
================
* **跨 Skill 冲突**：触发条件重叠但 policies / 输出契约矛盾 ⇒ **conflict finding**；
* **检索阶段对冲突对降权并提示**；
* **复用 T08 的冲突检测接口** —— 本模块**不重写规则**，直接调用
  :func:`forgeflow.skills.candidate_gates.detect_conflicts`（该函数被 T08 明确设计为
  「standalone, critic-independent」以便 T35 复用，见其 docstring）。

裁定（T35）
-----------
* 「降权」作用于检索命中的 **score**（乘一个 < 1 的因子），**不丢弃**命中 ——
  冲突未必意味着不可用，只是应排在非冲突项之后；提示以**显式 notices** 返回，不做
  静默处理（诚实纪律）。
* 冲突 finding 与 T08 同形（``{"code","severity","message","field"}``），UI 可统一渲染。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from forgeflow.skills.candidate_gates import CONFLICT_CODES, detect_conflicts

__all__ = [
    "CONFLICT_CODES",
    "CONFLICT_DOWNWEIGHT_FACTOR",
    "ConflictPair",
    "detect_skill_conflict",
    "scan_conflicts",
    "conflict_partner_ids",
    "is_conflicted",
    "apply_conflict_downweighting",
]

#: 冲突对在检索中的降权因子（内部默认 0.5；不是 §十 的校准项，属实现细节）。
CONFLICT_DOWNWEIGHT_FACTOR: float = 0.5


@dataclass(frozen=True)
class ConflictPair:
    """一对相互冲突的 skill 及其 conflict findings。"""

    left_skill_id: str
    right_skill_id: str
    findings: tuple[dict[str, Any], ...] = ()

    @property
    def codes(self) -> list[str]:
        return sorted({str(f.get("code")) for f in self.findings})

    @property
    def skill_ids(self) -> list[str]:
        return sorted({self.left_skill_id, self.right_skill_id})

    def to_dict(self) -> dict[str, Any]:
        return {
            "left_skill_id": self.left_skill_id,
            "right_skill_id": self.right_skill_id,
            "codes": self.codes,
            "findings": [dict(f) for f in self.findings],
        }


def _field(skill: Any, name: str) -> str:
    return str(getattr(skill, name, "") or "")


def _contract_source(skill: Any, specs: Mapping[str, Mapping[str, Any]] | None) -> Any:
    """Pick the value to hand to T08's ``detect_conflicts`` (a spec dict / contract).

    Prefers the current-version spec (``specs[skill_id]``), then ``skill.spec``, then
    the skill object itself (which T08 normalises to an empty contract if unusable).
    """
    skill_id = _field(skill, "id")
    if specs and skill_id in specs:
        return specs[skill_id]
    spec = getattr(skill, "spec", None)
    return spec if isinstance(spec, Mapping) else skill


def detect_skill_conflict(
    left: Any,
    right: Any,
    *,
    specs: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Thin wrapper over T08's reusable detector (same finding shape).

    Returns the conflict findings between two skills (possibly empty). The rules
    live in :func:`forgeflow.skills.candidate_gates.detect_conflicts` — this module
    never re-implements them.
    """
    return detect_conflicts(_contract_source(left, specs), _contract_source(right, specs))


def scan_conflicts(
    skills: Sequence[Any],
    *,
    specs: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[ConflictPair]:
    """Scan every unordered pair and return the conflicting ones (deterministic)."""
    by_id = {_field(s, "id"): s for s in skills if _field(s, "id")}
    ids = sorted(by_id)
    pairs: list[ConflictPair] = []
    for i, left_id in enumerate(ids):
        for right_id in ids[i + 1 :]:
            findings = detect_skill_conflict(
                by_id[left_id], by_id[right_id], specs=specs
            )
            if findings:
                pairs.append(
                    ConflictPair(
                        left_skill_id=left_id,
                        right_skill_id=right_id,
                        findings=tuple(dict(f) for f in findings),
                    )
                )
    return pairs


def conflict_partner_ids(pairs: Sequence[ConflictPair]) -> set[str]:
    """Every skill id that appears in at least one conflict pair."""
    out: set[str] = set()
    for pair in pairs:
        out.update(pair.skill_ids)
    return out


def is_conflicted(skill_id: str, pairs: Sequence[ConflictPair]) -> bool:
    return str(skill_id) in conflict_partner_ids(pairs)


def apply_conflict_downweighting(
    hits: Sequence[Any],
    pairs: Sequence[ConflictPair],
    *,
    factor: float = CONFLICT_DOWNWEIGHT_FACTOR,
) -> tuple[list[Any], list[str]]:
    """Down-weight retrieval hits that belong to a conflicting pair.

    Returns ``(adjusted_hits, notices)`` — hits are never dropped, only their
    ``score`` is multiplied by ``factor`` (< 1). ``notices`` records, per affected
    skill, which partners it conflicts with and the finding codes (the「提示」要求).
    """
    conflicted = conflict_partner_ids(pairs)
    if not conflicted:
        return list(hits), []

    by_skill: dict[str, list[ConflictPair]] = {}
    for pair in pairs:
        for sid in pair.skill_ids:
            by_skill.setdefault(sid, []).append(pair)

    out: list[Any] = []
    notices: list[str] = []
    for hit in hits:
        skill = getattr(hit, "skill", None)
        skill_id = _field(skill, "id")
        if skill_id not in conflicted:
            out.append(hit)
            continue
        related = by_skill.get(skill_id, [])
        partners = sorted({p for pair in related for p in pair.skill_ids if p != skill_id})
        codes = sorted({c for pair in related for c in pair.codes})
        adjusted = replace(hit, score=float(getattr(hit, "score", 0.0)) * factor)
        out.append(adjusted)
        notices.append(
            f"skill {skill_id} 与 {', '.join(partners)} 存在冲突（{', '.join(codes)}）："
            f"检索分已降权 ×{factor}"
        )
    return out, notices
