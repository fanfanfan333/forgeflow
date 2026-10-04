"""INC46 T25 — 追问指令解析（``documents/followup.py``）。

把用户在**已批准版本**之上的一句自然语言追问，解析成三种**确定**的版本链动作：

  * ``refine`` —— 基于最近一个 committed 版本继续改（「再正式一点」/「润色一下」）；
  * ``revert`` —— 回退到某一版（「回到第 1 版」/「撤销到 v2」）；
  * ``compare`` —— 任意两版本对比（「对比 v1 和 v3」）。

设计纪律
--------
* **只做确定性解析，不做猜测**。revert / compare 必须能从文本里**取到**版本号；
  取不到就抛 :class:`FollowUpParseError`（fail-closed），绝不「默认回退到上一版」
  或「随便挑两个版本」——那会把一次失败的意图理解伪装成一次成功的回退
  （红线 3 的同一条精神：不得折算）。
* 解析是**纯函数**，不碰任何 store，可单独断言。
* 优先级 ``compare > revert > refine``：一句「对比一下第 1 版和第 3 版」里
  既有「对比」也可能含「版」字，compare 必须先判。

``refine`` 的 ``base`` 是「最近 committed 版本」，因而是**运行时**事实，不在解析期
决定 —— 解析结果只给出 ``kind=refine``，基线由 :func:`forgeflow.documents.version_chain.refine`
用乐观锁对 head 校验。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "KIND_REFINE",
    "KIND_REVERT",
    "KIND_COMPARE",
    "KIND_UNKNOWN",
    "FOLLOWUP_KINDS",
    "FollowUp",
    "FollowUpParseError",
    "parse_followup",
]

KIND_REFINE = "refine"
KIND_REVERT = "revert"
KIND_COMPARE = "compare"
KIND_UNKNOWN = "unknown"
FOLLOWUP_KINDS: tuple[str, ...] = (KIND_REFINE, KIND_REVERT, KIND_COMPARE, KIND_UNKNOWN)


class FollowUpParseError(ValueError):
    """A revert / compare intent was recognised but its version numbers are missing.

    Fail-closed: we refuse rather than invent a target version.
    """

    def __init__(self, kind: str, text: str) -> None:
        super().__init__(f"{kind} intent needs explicit version number(s): {text!r}")
        self.kind = kind
        self.text = text


#: compare verbs — checked first (a compare sentence may also contain 「版」).
_COMPARE_RE = re.compile(r"(对比|比较|比对|差异|diff|不同)")
#: revert verbs.
_REVERT_RE = re.compile(r"(回退|还原|撤销|恢复到|退回|回到)")
#: refine verbs (继续改 / 再… / 润色 / 更…).
_REFINE_RE = re.compile(
    r"(再|更|继续|修改|调整|润色|精简|详细|正式|改写|重写|改一?下|优化|补充)"
)
#: A version token: ``v3`` / ``V 3`` / ``第3版`` / ``版本3`` / ``第 3 版``.
_VERSION_RE = re.compile(r"(?:v|V)\s*(\d+)|(?:版本|第)\s*(\d+)\s*版?")


@dataclass(frozen=True)
class FollowUp:
    """One parsed follow-up instruction."""

    kind: str
    raw: str
    to_version: int | None = None
    a: int | None = None
    b: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "raw": self.raw,
            "to_version": self.to_version,
            "a": self.a,
            "b": self.b,
        }


def _versions(text: str) -> list[int]:
    """Every explicit version number in ``text``, in order of appearance."""
    found: list[int] = []
    for m in _VERSION_RE.finditer(text):
        digit = m.group(1) or m.group(2)
        if digit is not None:
            found.append(int(digit))
    return found


def parse_followup(text: str) -> FollowUp:
    """Parse a follow-up instruction into a :class:`FollowUp`.

    Returns ``kind=unknown`` for anything that is not a recognised version-chain
    instruction (the caller decides how to handle it). Raises
    :class:`FollowUpParseError` when an intent is recognised but its version
    numbers are missing — never a silent default.
    """
    raw = text or ""
    numbers = _versions(raw)

    if _COMPARE_RE.search(raw):
        if len(numbers) < 2:
            raise FollowUpParseError(KIND_COMPARE, raw)
        return FollowUp(kind=KIND_COMPARE, raw=raw, a=numbers[0], b=numbers[1])

    if _REVERT_RE.search(raw):
        if not numbers:
            raise FollowUpParseError(KIND_REVERT, raw)
        return FollowUp(kind=KIND_REVERT, raw=raw, to_version=numbers[0])

    if _REFINE_RE.search(raw):
        return FollowUp(kind=KIND_REFINE, raw=raw)

    return FollowUp(kind=KIND_UNKNOWN, raw=raw)
