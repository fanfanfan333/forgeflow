"""T16 — the outcome label vocabulary and the success-rate arithmetic.

规格（任务书 T16）
------------------
``outcome_label`` 取值八选一::

    ACCEPTED_EXPLICIT   用户批准 / 点赞 / 明确采纳
    ACCEPTED_IMPLICIT   硬验证通过且窗口 W 内无返工（默认 W = 24h）
    REJECTED            用户拒绝
    REVISED             同会话对同一目标**再次修改**
    REVERTED            回退到更早版本
    ABANDONED           用户放弃
    FAILED_SYSTEM       run 状态为 failed_validation（由 T24 状态常量派生，无需用户信号）
    UNKNOWN             无任何信号

``hard_pass`` 取自 T23 验证栈：``True`` / ``False`` / ``None``（未测量）。

两个必须分开的概念（否则会踩红线 12）
--------------------------------------
1. **成功率**（统计口径）：``ACCEPTED_EXPLICIT`` 与 ``ACCEPTED_IMPLICIT`` 都算「接受」，
   都进分子；``UNKNOWN`` **既不进分子也不进分母**。全为 ``UNKNOWN`` ⇒ 成功率 ``None``，
   **不是** ``0``（红线 4：未测量 ⇒ None）。
2. **Miner 准入**（学习口径）：成功样本**只吃 ``ACCEPTED_EXPLICIT``**。
   ``ACCEPTED_IMPLICIT`` **仅用于统计，默认不纳入** —— 待 T36 校准后再定
   （§十 初始默认值表 A10）。``REJECTED`` / ``REVISED`` / ``REVERTED`` /
   ``FAILED_SYSTEM`` 作为负样本（失败模式）。

这两者**不得混用**：把 ``ACCEPTED_IMPLICIT`` 当成 Miner 成功样本，等于让
「没人抱怨」冒充「用户认可」。
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Mapping

__all__ = [
    "ACCEPTED_EXPLICIT",
    "ACCEPTED_IMPLICIT",
    "REJECTED",
    "REVISED",
    "REVERTED",
    "ABANDONED",
    "FAILED_SYSTEM",
    "UNKNOWN",
    "OUTCOME_LABELS",
    "RATE_POSITIVE",
    "MINER_POSITIVE",
    "MINER_NEGATIVE",
    "IMPLICIT_WINDOW_HOURS",
    "counts_toward_rate",
    "is_miner_positive",
    "is_miner_negative",
    "success_rate",
    "label_distribution",
]

ACCEPTED_EXPLICIT = "ACCEPTED_EXPLICIT"
ACCEPTED_IMPLICIT = "ACCEPTED_IMPLICIT"
REJECTED = "REJECTED"
REVISED = "REVISED"
REVERTED = "REVERTED"
ABANDONED = "ABANDONED"
FAILED_SYSTEM = "FAILED_SYSTEM"
UNKNOWN = "UNKNOWN"

#: The complete label set. Anything outside it is a bug, not a new label.
OUTCOME_LABELS: frozenset[str] = frozenset(
    {
        ACCEPTED_EXPLICIT,
        ACCEPTED_IMPLICIT,
        REJECTED,
        REVISED,
        REVERTED,
        ABANDONED,
        FAILED_SYSTEM,
        UNKNOWN,
    }
)

#: 统计口径的「接受」—— 显式与隐式都算（仅用于成功率，不用于 Miner）。
RATE_POSITIVE: frozenset[str] = frozenset({ACCEPTED_EXPLICIT, ACCEPTED_IMPLICIT})

#: **Miner 成功样本**只吃显式接受（A10：隐式默认不纳入，T36 校准后再定）。
MINER_POSITIVE: frozenset[str] = frozenset({ACCEPTED_EXPLICIT})

#: 负样本（失败模式）—— 供学习链路与失败模式聚类使用。
#: 注意 ``ABANDONED`` **不在**其中：用户放弃 ≠ 结果失败，它是「无结论」，
#: 与 UNKNOWN 一样不进 Miner 的任一侧，避免把「没用」当「用错了」。
MINER_NEGATIVE: frozenset[str] = frozenset({REJECTED, REVISED, REVERTED, FAILED_SYSTEM})

#: 隐式接受窗口的初始默认值（§十：W = 24h，T36 校准后再定）。
IMPLICIT_WINDOW_HOURS: int = 24


def counts_toward_rate(label: str | None) -> bool:
    """该 run 是否进入成功率的**分母**。

    ``UNKNOWN``（含 ``None`` = 尚未标注）一律不计入 —— 红线 12：
    未知结果不等于成功，也不得因为「不知道」而稀释成功率。
    """
    return label is not None and label != UNKNOWN


def is_miner_positive(label: str | None) -> bool:
    """是否可作为 Miner 的**成功样本**（只认显式接受）。"""
    return label in MINER_POSITIVE


def is_miner_negative(label: str | None) -> bool:
    """是否作为 Miner 的**负样本**（失败模式）。"""
    return label in MINER_NEGATIVE


def success_rate(labels: Iterable[str | None]) -> float | None:
    """成功率 = 接受数 / **已定标签**数；全为 UNKNOWN ⇒ ``None``。

    * 分子：``ACCEPTED_EXPLICIT`` + ``ACCEPTED_IMPLICIT``
    * 分母：所有**非 UNKNOWN** 的标签（``UNKNOWN`` 既不进分子也不进分母）
    * 分母为 0 ⇒ ``None`` —— **绝不返回 0**（红线 4：不得用默认 0 掩盖缺失指标）

    Args:
        labels: 任意可迭代的标签序列（允许 ``None`` = 未标注）。

    Returns:
        ``float`` ∈ [0, 1]，或 ``None``（无可用样本 / 全部未知）。
    """
    counted = [label for label in labels if counts_toward_rate(label)]
    if not counted:
        return None
    positive = sum(1 for label in counted if label in RATE_POSITIVE)
    return positive / len(counted)


def label_distribution(labels: Iterable[str | None]) -> dict[str, int]:
    """每个标签的出现次数（``None`` 归入 ``UNKNOWN``）。

    输出的键**恒为** :data:`OUTCOME_LABELS` 的全部八个标签（缺失者为 0），
    这样分布样例在不同 run 集之间可直接比较，不会因为某标签恰好为 0 而缺列。
    """
    counter: Counter[str] = Counter()
    for label in labels:
        counter[UNKNOWN if label is None else label] += 1
    return {label: counter.get(label, 0) for label in sorted(OUTCOME_LABELS)}


def miner_sample_ids(
    labels: Mapping[str, str | None],
) -> tuple[list[str], list[str]]:
    """把 ``run_id -> label`` 切成 ``(成功样本 id, 负样本 id)``。

    ``UNKNOWN`` / ``ABANDONED`` / ``ACCEPTED_IMPLICIT`` **两侧都不进** ——
    这是准入过滤的唯一权威实现，Miner 必须调它而不是自己判等。
    """
    positives = [run_id for run_id, label in labels.items() if is_miner_positive(label)]
    negatives = [run_id for run_id, label in labels.items() if is_miner_negative(label)]
    return positives, negatives
