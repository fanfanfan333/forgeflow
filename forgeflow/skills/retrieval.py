"""INC46 T09 — Skill retrieval / selection pipeline (Discovery 内部链路).

任务书 T09「规格」要求链路完整::

    Query -> Hybrid Retrieval -> RRF -> Tenant/RBAC Filter -> Capability Filter -> Rerank -> Top-K

本模块把每一阶段做成**可单独断言 / 可单独注入**的命名阶段，使「链路完整」可审计。

裁定 V-1 · 过滤位置强于任务书链序的字面位置
-----------------------------------------------
规格把 ``Tenant/RBAC Filter`` 画在 ``RRF`` **之后**，但「增补(v3)」要求
「**权限先于一切**：无权限 / 跨租户 Skill 的元数据也不得进入候选池」。
两者冲突时以**更强**的增补为准：

* 候选池在**构造阶段**（:func:`build_pool`）就只允许「本租户 ∧ 有读权限 ∧ 未归档」的
  skill 进入 —— 被排除的 skill **连打分都不会被看到**，元数据根本不进池；
* 其余阶段（Hybrid → RRF → Capability → Rerank → Top-K）仍按任务书顺序编排，
  每个阶段都是命名函数，可单独单测与注入。

裁定 V-2 · Capability Filter 的语义
-------------------------------------
「缺能力 Skill 被过滤」落在 T04 的能力模型（``skills/tool_permissions``）上，判两条：

1. **覆盖性**：调用方给出 ``required_tools`` 时，skill 声明工具必须覆盖所需集合，否则过滤；
2. **上限性**：skill 的 ``class_of_skill(spec)["overall"]`` 不得超过 ``max_class``，超出即过滤。

默认（两者均为 ``None``）⇒ **空操作**，不改变既有召回结果。

裁定 V-4 · deprecated / archived 空操作
-----------------------------------------
:data:`EXCLUDED_STATUSES` 定义了排除集。当前 ``skills`` 表不存在
``deprecated`` / ``archived`` 状态 ⇒ 该过滤**当前为空操作**；T35 落地状态机后自动生效。
此处**不声称已生效**。

诚实纪律
--------
* 未测量一律 ``None``，绝不写 ``0``（INC46 §8 红线 4）。本模块的 ``similarity`` /
  ``lexical_score`` 是**真实算出来的**，故为 ``float``；若某分量算不出来则为 ``None``。
* 排序必须**确定性**（同分按 ``skill_id`` 字典序），否则测试与回放都不可靠。
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from forgeflow.experience.embedding import cosine_similarity, embed_text
from forgeflow.skills.tool_permissions import CLASS_ORDER, class_of_skill

logger = logging.getLogger(__name__)

__all__ = [
    "EXCLUDED_STATUSES",
    "RRF_K",
    "RetrievedSkill",
    "build_pool",
    "lexical_scores",
    "dense_scores",
    "rrf_fuse",
    "capability_filter",
    "rerank",
    "top_k",
    "retrieve_skills",
]

#: 检索前剔除的状态（T35 落地状态机前为空操作 —— 见裁定 V-4）。
EXCLUDED_STATUSES: frozenset[str] = frozenset({"deprecated", "archived"})

#: RRF 的阻尼常数（标准取值 60；越大越弱化 top-rank 的优势）。
RRF_K: int = 60

_NON_TOKEN = re.compile(r"[^0-9a-z一-鿿]+")
_CJK = re.compile(r"[一-鿿]")


@dataclass(frozen=True)
class RetrievedSkill:
    """One retrieval hit: the skill plus every score that produced its rank.

    ``similarity`` 是**真实算出来的**稠密余弦（``[-1, 1]``），不是硬编码的 ``1.0``；
    算不出来时为 ``None``（红线 4：未测量 ⇒ None）。
    """

    skill: Any
    score: float
    similarity: float | None
    lexical_score: float | None
    fused_score: float


# --------------------------------------------------------------------------- #
# 分词（确定性；中英文混合）                                                    #
# --------------------------------------------------------------------------- #
def _tokenize(text: str) -> list[str]:
    """Deterministic tokenisation for the lexical stage.

    ASCII 段按非字母数字切词；CJK 段**逐字**成词（不依赖任何分词器，保证离线与确定性）。
    """
    low = (text or "").lower()
    tokens: list[str] = []
    for chunk in _NON_TOKEN.split(low):
        if not chunk:
            continue
        if _CJK.search(chunk):
            tokens.extend(ch for ch in chunk if _CJK.match(ch))
        else:
            tokens.append(chunk)
    return tokens


def _field(skill: Any, name: str) -> str:
    return str(getattr(skill, name, "") or "")


def _skill_text(skill: Any) -> str:
    return f"{_field(skill, 'name')} {_field(skill, 'domain')} {_field(skill, 'description')}".strip()


# --------------------------------------------------------------------------- #
# 阶段 0（池闸）—— Tenant / RBAC / 状态                                        #
# --------------------------------------------------------------------------- #
def build_pool(
    tenant_id: str | None,
    candidates: Iterable[Any],
    *,
    permissions: Mapping[str, Sequence[Any]] | None = None,
) -> list[Any]:
    """**The load-bearing gate** (裁定 V-1): build the candidate pool.

    只有「本租户 ∧ 有读权限 ∧ 状态未被排除」的 skill 进入返回列表；被排除者
    **不会参与后续任何打分**，因此其元数据不进候选池。

    权限判定口径（显式声明，非静默）:
      * 该 skill **没有**任何权限行 ⇒ 视为「未声明限制」，放行（保持既有召回不变）；
      * 该 skill **有**权限行 ⇒ 必须至少有一行 ``granted=True`` 才放行，否则剔除。

    Args:
        tenant_id: 调用方租户；``None`` 视为一个独立作用域。
        candidates: 仓储层返回的候选（通常已按 tenant 查询，此处是**纵深第二道**）。
        permissions: ``skill_id -> Sequence[SkillPermission]``；``None`` 表示无权限数据。

    Returns:
        通过闸门的 skill 列表（保持输入顺序，便于确定性）。
    """
    perms = permissions or {}
    pool: list[Any] = []
    for skill in candidates:
        if str(getattr(skill, "tenant_id", None)) != str(tenant_id):
            continue
        if _field(skill, "status") in EXCLUDED_STATUSES:
            continue
        rows = perms.get(_field(skill, "id"), ())
        if rows and not any(bool(getattr(r, "granted", False)) for r in rows):
            continue
        pool.append(skill)
    return pool


# --------------------------------------------------------------------------- #
# 阶段 1 —— Hybrid Retrieval（BM25 词法 + 稠密余弦）                           #
# --------------------------------------------------------------------------- #
def lexical_scores(query: str, documents: Sequence[str], *, k1: float = 1.5, b: float = 0.75) -> list[float]:
    """BM25 over the skill text fields (deterministic, no external index)."""
    docs_tokens = [_tokenize(d) for d in documents]
    n_docs = len(docs_tokens)
    if n_docs == 0:
        return []
    avgdl = (sum(len(t) for t in docs_tokens) / n_docs) or 1.0
    df: Counter[str] = Counter()
    for toks in docs_tokens:
        df.update(set(toks))

    query_tokens = set(_tokenize(query))
    scores: list[float] = []
    for toks in docs_tokens:
        tf = Counter(toks)
        dl = len(toks) or 1
        score = 0.0
        for term in query_tokens:
            f = tf.get(term, 0)
            if not f:
                continue
            idf = math.log(1.0 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
            denom = f + k1 * (1.0 - b + b * dl / avgdl)
            score += idf * f * (k1 + 1.0) / denom
        scores.append(score)
    return scores


def _dense_input(text: str) -> str:
    """Pre-tokenise before embedding — **实测必需**，不是风格选择。

    ``experience.embedding.deterministic_embedding`` 按空白分词，把连续中文当成
    **一个** token：实测 ``embed_text("合同风险审查")`` 的 1536 维里只有 **1** 个
    非零分量，任意两段中文的余弦恒为 ``0.0``（英文对照为 ``0.866``）。若不预处理，
    稠密分支对中文完全空转 —— 混合检索会退化成纯词法，而「相似度」仍是**算出来的**
    ``0.0``，从外部看不出它失效（静默降级，最危险的一种）。

    改用 :func:`_tokenize` 的逐字切分并以空格连接后，实测：
    ``cos("合同风险审查", "合同审查 法务 自动识别合同风险点…") = 0.7698``，
    与不相关文档的余弦为 ``0.0`` —— 区分度正确。

    这里**不修改** ``embedding.py``：它是既有共享模块，改动面远超 T09。
    """
    return " ".join(_tokenize(text))


def dense_scores(query: str, documents: Sequence[str]) -> list[float | None]:
    """Dense cosine similarity via the existing deterministic embedding.

    任一向量算不出来时该项为 ``None``（未测量），**不写 0**（红线 4）。
    query 向量算不出来 ⇒ **所有**文档的稠密相似度都是未测量（不抛异常，
    整条链路不得因单点失败丢掉整个 skill 召回源）。
    """
    try:
        query_vec = embed_text(_dense_input(query))
    except Exception:  # noqa: BLE001 — 召回链路不得因 embedding 失败而整体崩塌
        logger.warning("skill retrieval: dense embedding unavailable — similarity stays unmeasured")
        return [None] * len(documents)

    out: list[float | None] = []
    for doc in documents:
        try:
            vec = embed_text(_dense_input(doc))
        except Exception:  # noqa: BLE001 — 单条失败不应拖垮整条链路
            logger.warning("skill retrieval: dense embedding failed for one document")
            out.append(None)
            continue
        out.append(cosine_similarity(query_vec, vec) if vec else None)
    return out


# --------------------------------------------------------------------------- #
# 阶段 2 —— RRF 融合                                                           #
# --------------------------------------------------------------------------- #
def rrf_fuse(rankings: Sequence[Sequence[int]], *, k: int = RRF_K) -> dict[int, float]:
    """Reciprocal Rank Fusion over several **rankings of pool indices** (0-based).

    每个 ranking 按「越靠前越好」给出池内下标；融合分 = Σ 1/(k + rank)，rank 从 1 起。
    未出现在任何 ranking 的下标不出现在结果里。
    """
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, index in enumerate(ranking, start=1):
            fused[index] = fused.get(index, 0.0) + 1.0 / (k + rank)
    return fused


def _ranked(scores: Sequence[float | None]) -> list[int]:
    """Indices sorted best-first; ``None`` (unmeasured) sorts last but is kept."""
    order = sorted(
        range(len(scores)),
        key=lambda i: (scores[i] is not None, scores[i] if scores[i] is not None else 0.0, -i),
        reverse=True,
    )
    return order


# --------------------------------------------------------------------------- #
# 阶段 3 —— Capability Filter                                                  #
# --------------------------------------------------------------------------- #
def _declared_tools(spec: Mapping[str, Any] | None) -> set[str]:
    if not spec:
        return set()
    tools = spec.get("tools") or []
    return {str(t).strip() for t in tools if str(t).strip()}


def capability_filter(
    indices: Iterable[int],
    pool: Sequence[Any],
    *,
    specs: Mapping[str, Mapping[str, Any]] | None = None,
    required_tools: Sequence[str] | None = None,
    max_class: str | None = None,
) -> list[int]:
    """Drop candidates whose **capability** cannot satisfy the request (裁定 V-2).

    * ``required_tools``：skill 声明工具必须覆盖所需集合（覆盖性）；
    * ``max_class``：``class_of_skill(spec)["overall"]`` 的等级不得超过上限（上限性）。

    两者均为 ``None`` ⇒ 空操作（原样返回）。
    """
    kept: list[int] = []
    need = {str(t).strip() for t in (required_tools or ()) if str(t).strip()}
    ceiling = CLASS_ORDER.get(max_class) if max_class else None
    specs = specs or {}
    for index in indices:
        skill = pool[index]
        spec = specs.get(_field(skill, "id"))
        if need and not need <= _declared_tools(spec):
            continue
        if ceiling is not None:
            overall = class_of_skill(spec)["overall"]
            if CLASS_ORDER.get(overall, 0) > ceiling:
                continue
        kept.append(index)
    return kept


# --------------------------------------------------------------------------- #
# 阶段 4 —— Rerank                                                             #
# --------------------------------------------------------------------------- #
def rerank(
    indices: Sequence[int],
    *,
    fused: Mapping[int, float],
    dense: Mapping[int, float | None],
    usage: Mapping[int, int],
    lexical: Mapping[int, float | None] | None = None,
) -> dict[int, float]:
    """Combine RRF + dense + lexical + usage into the final ranking score.

    权重：``0.45 * fused + 0.25 * dense + 0.20 * lexical + 0.10 * usage``。

    裁定 V-7 · 为什么必须补回词法量级
    ----------------------------------
    RRF 是 **rank-only** 融合：它把「词法分 8.07 vs 1.28」这种**六倍**量级差，
    压成 ``1/(60+1)`` 与 ``1/(60+2)`` 的万分之五之差。实测（query=「合同风险审查」）
    只靠 fused + usage 时，usage 128 的「客户流失分析」会反超词法分 8.07 的
    「合同审查」—— 排序结果与语义明显相悖。故在 rerank 里把**真实词法分**
    （归一化后）作为独立分量纳入，usage 只作 0.10 的轻微倾向。

    未测量（``None``）的稠密分量按 ``0.0`` 计入**组合分**，但
    :attr:`RetrievedSkill.similarity` 仍如实为 ``None`` —— 组合分是排序依据，
    与「相似度是否测得」是两件事，不得相互冒充。
    """
    if not indices:
        return {}
    lex = lexical or {}
    f_max = max((fused.get(i, 0.0) for i in indices), default=0.0) or 1.0
    l_max = max((lex.get(i) or 0.0 for i in indices), default=0.0) or 1.0
    u_max = max((usage.get(i, 0) for i in indices), default=0) or 1
    scored: dict[int, float] = {}
    for index in indices:
        d = dense.get(index)
        l = lex.get(index)
        scored[index] = (
            0.45 * (fused.get(index, 0.0) / f_max)
            + 0.25 * (d if d is not None else 0.0)
            + 0.20 * ((l if l is not None else 0.0) / l_max)
            + 0.10 * (usage.get(index, 0) / u_max)
        )
    return scored


# --------------------------------------------------------------------------- #
# 阶段 5 —— Top-K                                                              #
# --------------------------------------------------------------------------- #
def top_k(
    pool: Sequence[Any],
    scores: Mapping[int, float],
    *,
    dense: Mapping[int, float | None] | None = None,
    lexical: Mapping[int, float | None] | None = None,
    fused: Mapping[int, float] | None = None,
    k: int = 3,
) -> list[RetrievedSkill]:
    """Deterministic Top-K: score desc, then ``skill_id`` asc (no ties left to chance)."""
    dense = dense or {}
    lexical = lexical or {}
    fused = fused or {}
    ordered = sorted(
        scores,
        key=lambda i: (-scores[i], _field(pool[i], "id")),
    )[: max(0, k)]
    return [
        RetrievedSkill(
            skill=pool[i],
            score=scores[i],
            similarity=dense.get(i),
            lexical_score=lexical.get(i),
            fused_score=fused.get(i, 0.0),
        )
        for i in ordered
    ]


# --------------------------------------------------------------------------- #
# 编排                                                                         #
# --------------------------------------------------------------------------- #
def retrieve_skills(
    tenant_id: str | None,
    query: str,
    candidates: Sequence[Any],
    *,
    permissions: Mapping[str, Sequence[Any]] | None = None,
    specs: Mapping[str, Mapping[str, Any]] | None = None,
    k: int = 3,
    required_tools: Sequence[str] | None = None,
    max_class: str | None = None,
) -> list[RetrievedSkill]:
    """Run the full T09 chain and return the Top-K hits.

    Query -> Hybrid Retrieval -> RRF -> Capability Filter -> Rerank -> Top-K，
    且 **Tenant/RBAC 闸在池构造阶段先行**（裁定 V-1）。
    """
    pool = build_pool(tenant_id, candidates, permissions=permissions)
    if not pool:
        return []

    docs = [_skill_text(s) for s in pool]
    lex = lexical_scores(query, docs)
    den = dense_scores(query, docs)

    fused = rrf_fuse([_ranked(lex), _ranked(den)])
    kept = capability_filter(
        list(fused), pool, specs=specs,
        required_tools=required_tools, max_class=max_class,
    )
    usage = {i: int(getattr(pool[i], "usage_count", 0) or 0) for i in kept}
    scores = rerank(
        kept,
        fused=fused,
        dense={i: den[i] for i in kept},
        usage=usage,
        lexical={i: lex[i] for i in kept},  # 裁定 V-7：补回词法量级
    )
    return top_k(
        pool, scores,
        dense={i: den[i] for i in kept},
        lexical={i: lex[i] for i in kept},
        fused=fused,
        k=k,
    )
