"""INC46 T29 — 每步 token 记账（Step Token Accounting）。

任务书 T29 要求
----------------
「token 记账：每步记录 prompt / completion token，供 T36 的成本指标；**未记账 ⇒ None**。」

本模块把「一步的 token 账」固化成一个**诚实**的值对象 :class:`StepTokenAccounting`，
并给出把它**加性**塞进既有 ``run_steps`` payload 的投影 :func:`run_step_token_payload`。

诚实纪律（INC46 §8 红线 4）
---------------------------
* **未测量 ⇒ ``None``，禁止写 ``0``。** 一条没有计数来源的 prompt / completion 一律记
  ``None``（「不知道多少 token」），**绝不**用 ``0`` 冒充（那会谎报「这一步没花 token」）。
* 「**已测量的 0**」与「未测量」是两件事，不得相互冒充：显式传入的 ``0``（真的数到
  0 个 token）保持 ``0``；``None`` 输入保持 ``None``。
* token 计数**复用**既有的字符启发式 :func:`forgeflow.experience.token_budget.estimate_tokens`
  （**非精确 tokenizer**），因此字段名带 ``_tokens`` 语义即「估算」，绝不冒充精确 BPE 计数。

与 T36 的接缝
-------------
:func:`run_step_token_payload` 返回的 dict 可直接**加性**并入 ``run_steps`` 行的
``output`` / ``payload`` —— 它只新增一个 ``"token_accounting"`` 键，且 ``None`` 原样保留
（不写 0）。既有 payload 字段一概不动。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forgeflow.context.budget_accounting import TokenCounter, measure_tokens
from forgeflow.experience.token_budget import estimate_tokens

__all__ = [
    "StepTokenAccounting",
    "account_step",
    "run_step_token_payload",
    "TOKEN_ACCOUNTING_KEY",
]

#: 并入 ``run_steps`` payload 时使用的键名（加性，不覆盖既有键）。
TOKEN_ACCOUNTING_KEY = "token_accounting"


@dataclass(frozen=True)
class StepTokenAccounting:
    """One step's token accounting — ``None`` means **not measured** (never ``0``).

    Attributes:
        prompt_tokens: 该步的 prompt token 数；未记账 ⇒ ``None``。
        completion_tokens: 该步的 completion token 数；未记账 ⇒ ``None``。
        context_tokens: 该步进模型的上下文 token 数（若测量）；未记账 ⇒ ``None``。
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    context_tokens: int | None = None

    def measured(self) -> bool:
        """True when **at least one** of the three quantities was actually measured.

        「一个都没测到」与「测得都是 0」是两件事；本方法只回答前者。
        """
        return any(
            v is not None
            for v in (self.prompt_tokens, self.completion_tokens, self.context_tokens)
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection that **preserves ``None``** (never coerces to 0)."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "context_tokens": self.context_tokens,
            "measured": self.measured(),
        }


def _coerce(value: str | int | None, counter: TokenCounter | None) -> int | None:
    """Normalise a raw token input to ``int`` (measured) or ``None`` (unmeasured).

    * ``None`` ⇒ ``None``（未测量，绝不写 0）；
    * ``int``（含 **0**）⇒ 原样返回（``0`` 是「已测量的 0」）；
    * ``str`` ⇒ 走 :func:`measure_tokens`（默认字符启发式）；空串 ⇒ 已测量的 0。
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool 是 int 子类，显式拒绝以免 0/1 冒充
        raise TypeError("token 记账不接受 bool（0/1 会冒充测量值）")
    if isinstance(value, int):
        return int(value)
    return measure_tokens(str(value), counter=counter)


def account_step(
    *,
    prompt: str | int | None = None,
    completion: str | int | None = None,
    context: str | int | None = None,
    counter: TokenCounter | None = estimate_tokens,
) -> StepTokenAccounting:
    """Build a :class:`StepTokenAccounting` from raw step inputs.

    Args:
        prompt: prompt 正文（或已测得的 token 数）；``None`` ⇒ 未记账（``None``）。
        completion: completion 正文（或已测得的数）；``None`` ⇒ 未记账（``None``）。
        context: 该步上下文正文（或已测得的数）；``None`` ⇒ 未记账（``None``）。
        counter: 计数器；``None`` ⇒ 文本输入一律**未测量**（``None``）。默认 = 既有
            字符启发式估算器（**非精确 tokenizer**）。

    Returns:
        :class:`StepTokenAccounting`；未测量的量为 ``None``，绝不写 ``0``。
    """
    return StepTokenAccounting(
        prompt_tokens=_coerce(prompt, counter),
        completion_tokens=_coerce(completion, counter),
        context_tokens=_coerce(context, counter),
    )


def run_step_token_payload(
    accounting: StepTokenAccounting | None,
    *,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project ``accounting`` into an **additive** ``run_steps`` payload fragment.

    The returned dict is ``base`` (a copy) plus exactly one new key
    :data:`TOKEN_ACCOUNTING_KEY`. An unmeasured field stays ``None`` inside it —
    the payload never fabricates a ``0``. When ``accounting`` is ``None`` the
    fragment reports an **unmeasured** accounting (all ``None``), so a downstream
    cost metric (T36) can tell "not accounted" from "zero cost".

    Args:
        accounting: 该步的账；``None`` ⇒ 记一条**未测量**账（三量皆 ``None``）。
        base: 既有 payload（原样保留，**不被修改**）。

    Returns:
        一个新的 dict：``{**base, "token_accounting": {...}}``。
    """
    payload: dict[str, Any] = dict(base or {})
    record = accounting if accounting is not None else StepTokenAccounting()
    payload[TOKEN_ACCOUNTING_KEY] = record.to_dict()
    return payload
