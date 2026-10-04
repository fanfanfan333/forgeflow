"""INC46 T34 — 灰度指标与置信下界（Rollout metrics + interval lower bound）。

任务书 T34 §规格（初始默认）
============================
* **阶段**：``5% → 25% → 100%``；每阶段需 ``≥ 30`` 个**有标签** run 且 ``≥ 24h``。
* **指标**：``success_rate``（T16 标签，仅有标签样本）、``validate_fail_rate``、
  ``rework_rate``（``REVISED`` + ``REVERTED``）、``P95`` 延迟、成本。
* **比较口径**：与 incumbent 比较时用**区间下界**（Wilson）而非点估计 ——
  小样本噪声不得触发「误回滚 / 误推进」。
* **回滚触发**（初始默认）：``success_rate`` 下降 > 5pp（置信下界）、或
  ``validate_fail_rate`` 上升 > 3pp、或 ``P95`` > 1.5×、或出现 DANGEROUS
  越权事件（立即）。
* **样本不足** ⇒ ``insufficient_data``：**不推进、也不判通过**（``None``）。

诚实纪律（红线 4 / 12）
-----------------------
任何**未测量**的量一律 ``None``，绝不写 0：

* 无已定标签（全 ``UNKNOWN``）⇒ ``success_rate is None``（**不是** 0）；
* 无延迟样本 ⇒ ``p95_latency_ms is None``；无成本样本 ⇒ ``cost is None``；
* 无窗口观测时长 ⇒ ``window_hours is None`` ⇒ 门槛判定为「样本不足」。

本模块纯函数、无 I/O、无时钟（时间由调用方传入），便于单独断言与反事实变异。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from forgeflow.outcomes.signals import (
    FAILED_SYSTEM,
    RATE_POSITIVE,
    REVERTED,
    REVISED,
    counts_toward_rate,
    label_distribution,
    success_rate as _success_rate,
)

__all__ = [
    "STAGES",
    "MIN_LABELED_RUNS",
    "MIN_WINDOW_HOURS",
    "SUCCESS_DROP_PP",
    "VALIDATE_FAIL_RISE_PP",
    "P95_LATENCY_RATIO",
    "Z_95",
    "MetricError",
    "wilson_lower_bound",
    "percentile",
    "RolloutMetrics",
    "derive_metrics",
]

#: 灰度阶段（初始默认）：5% → 25% → 100%。
STAGES: tuple[int, ...] = (5, 25, 100)

#: 每阶段门槛（初始默认）：≥ 30 个有标签 run 且 ≥ 24h 观测窗口。
MIN_LABELED_RUNS: int = 30
MIN_WINDOW_HOURS: float = 24.0

#: 回滚阈值（初始默认，任务书 T34 原文）。
SUCCESS_DROP_PP: float = 5.0
VALIDATE_FAIL_RISE_PP: float = 3.0
P95_LATENCY_RATIO: float = 1.5

#: 95% 正态分位（Wilson 区间用；显式常量便于反事实变异时逐字节替换）。
Z_95: float = 1.959963984540054


class MetricError(ValueError):
    """非法指标输入（如负样本数）——fail-closed，不静默纠偏。"""


def wilson_lower_bound(successes: int, n: int, *, z: float = Z_95) -> float | None:
    """成功率的 Wilson 置信**下界**（``z`` 默认 95%）。

    ``None`` 当 ``n <= 0``（未测量 ⇒ None，红线 4）。下界而非点估计，是为了
    让**小样本**区间更宽、更难越过「下降 > 5pp」的触发线 —— 这正是反事实
    「把置信下界改为点估计 ⇒ 小样本噪声用例必须转红」所钉住的承重语义。

    Raises:
        MetricError: ``n < 0`` 或 ``successes`` 不在 ``[0, n]``。
    """
    if n is None or n <= 0:
        return None
    if successes < 0 or n < 0 or successes > n:
        raise MetricError(f"非法计数：successes={successes} n={n}")
    phat = successes / n
    denom = 1.0 + z * z / n
    centre = phat + z * z / (2.0 * n)
    margin = z * math.sqrt(phat * (1.0 - phat) / n + z * z / (4.0 * n * n))
    lower = (centre - margin) / denom
    # 数值下界裁剪到 [0, 1]（Wilson 下界理论上恒 ≥ 0，此处仅防御浮点误差）。
    return max(0.0, min(1.0, lower))


def percentile(values: Sequence[float], q: float) -> float | None:
    """最近秩（nearest-rank）分位数；空序列 ⇒ ``None``（未测量）。"""
    xs = sorted(float(v) for v in values if v is not None)
    if not xs:
        return None
    if not 0.0 <= q <= 1.0:
        raise MetricError(f"非法分位 q={q}")
    # 最近秩：ceil(q * n) 的第 k 个（1-based），至少取第 1 个。
    k = max(1, math.ceil(q * len(xs)))
    return xs[k - 1]


@dataclass(frozen=True)
class RolloutMetrics:
    """某一版本在一个阶段窗口上的**实测**指标快照。

    字段为 ``None`` 即「未测量」，绝不写成 0（红线 4）。``insufficient`` 由
    **样本量**与**窗口时长**共同决定：任一未达门槛即样本不足，此时既不得
    推进也不得判通过（调用方据此走 ``insufficient_data``）。
    """

    labeled_runs: int = 0
    successes: int = 0
    success_rate: float | None = None
    success_rate_lb: float | None = None
    validate_fail_rate: float | None = None
    rework_rate: float | None = None
    p95_latency_ms: float | None = None
    cost: float | None = None
    window_hours: float | None = None

    @property
    def insufficient(self) -> bool:
        """样本不足（有标签 run < 30 或 窗口 < 24h 或 窗口未测量）。"""
        if self.window_hours is None:
            return True
        return self.labeled_runs < MIN_LABELED_RUNS or self.window_hours < MIN_WINDOW_HOURS

    def to_dict(self) -> dict[str, Any]:
        return {
            "labeled_runs": self.labeled_runs,
            "successes": self.successes,
            "success_rate": self.success_rate,
            "success_rate_lb": self.success_rate_lb,
            "validate_fail_rate": self.validate_fail_rate,
            "rework_rate": self.rework_rate,
            "p95_latency_ms": self.p95_latency_ms,
            "cost": self.cost,
            "window_hours": self.window_hours,
            "insufficient": self.insufficient,
        }


def derive_metrics(
    labels: Iterable[str | None],
    *,
    latencies_ms: Iterable[float] | None = None,
    costs: Iterable[float] | None = None,
    window_hours: float | None = None,
) -> RolloutMetrics:
    """从 run 的 outcome 标签（T16）推导灰度指标。

    * **分母** = 所有**非 UNKNOWN** 的标签（``UNKNOWN`` 既不进分子也不进分母）；
    * ``success_rate`` = ``ACCEPTED_*`` 占比；全 ``UNKNOWN`` ⇒ ``None``；
    * ``success_rate_lb`` = Wilson 下界（同分母/分子）；
    * ``validate_fail_rate`` = ``FAILED_SYSTEM`` / 分母；
    * ``rework_rate`` = (``REVISED`` + ``REVERTED``) / 分母；
    * ``p95_latency_ms`` = 延迟样本的 P95；无样本 ⇒ ``None``；
    * ``cost`` = 成本之和；无样本 ⇒ ``None``。

    Args:
        labels: run 的 ``outcome_label`` 序列（允许 ``None`` = 未标注）。
        latencies_ms: 各 run 的延迟（毫秒），可空。
        costs: 各 run 的成本，可空。
        window_hours: 该阶段的观测窗口时长；``None`` = 未测量。
    """
    seq = list(labels)
    counted = [label for label in seq if counts_toward_rate(label)]
    n = len(counted)
    successes = sum(1 for label in counted if label in RATE_POSITIVE)
    dist = label_distribution(seq)

    rate = _success_rate(seq)  # 复用 T16 的权威算术（分母为 0 ⇒ None）
    lb = wilson_lower_bound(successes, n) if n > 0 else None

    validate_fail_rate = (dist.get(FAILED_SYSTEM, 0) / n) if n > 0 else None
    rework_rate = ((dist.get(REVISED, 0) + dist.get(REVERTED, 0)) / n) if n > 0 else None

    lat = [float(x) for x in (latencies_ms or ()) if x is not None]
    p95 = percentile(lat, 0.95) if lat else None
    cost_vals = [float(x) for x in (costs or ()) if x is not None]
    total_cost = sum(cost_vals) if cost_vals else None

    return RolloutMetrics(
        labeled_runs=n,
        successes=successes,
        success_rate=rate,
        success_rate_lb=lb,
        validate_fail_rate=validate_fail_rate,
        rework_rate=rework_rate,
        p95_latency_ms=p95,
        cost=total_cost,
        window_hours=None if window_hours is None else float(window_hours),
    )
