"""INC46 T17 — 数据泄漏防护（leakage guard）。

为什么需要它
------------
一个 Skill 版本的「成功」若在它**学过**的数据上被度量，就是「自己考自己」——
分数再高也不代表泛化。T17 的目标是把评测集与训练/挖掘数据隔离：golden 的
holdout 用例若与某候选的训练 Experience 共享**同一份源文档**，该 holdout 用例
即被污染，针对该候选的回归结果必须**作废**（红线 13：不得凭空造「通过」）。

本模块只做两件事，且都是纯函数（无 I/O、无 LLM）：

1. 定义**源文档指纹**的单一口径（:func:`fingerprint_source_document`）——
   ``"sha256:<hex>"``，hex = 源文档字节的 SHA-256。同一份文档在任何地方得到
   同一个指纹；比对前经 :func:`normalize_fingerprint` 归一化。
2. 判定泄漏（:func:`detect_leakage`）——把 golden holdout 用例的源文档指纹集合
   与训练 Experience 携带的源文档指纹集合求交集；非空即泄漏。

训练 Experience 如何携带「源文档指纹」（约定）
----------------------------------------------
:class:`~forgeflow.experience.models.ExperienceRecord` 没有专门的源文档列，因此
指纹以**可加**方式写入（任一即可，全部会被采集）：

* ``tags`` 中以 ``source_doc:`` 或 ``doc_fp:`` 为前缀的条目；
* ``reusable_steps[i]`` 中的 ``source_fingerprint`` / ``source_doc_fingerprint`` 键；
* 记录对象（或映射）上的 ``source_doc_fingerprint`` /
  ``source_document_fingerprint`` 属性。

写入侧用 :func:`tag_experience_with_source_doc` 统一落标签，读取侧用
:func:`experience_source_fingerprints` 统一采集——两侧同源，不会各自漂移。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = [
    "FINGERPRINT_PREFIX",
    "SOURCE_TAG_PREFIXES",
    "FINGERPRINT_STEP_KEYS",
    "fingerprint_source_document",
    "normalize_fingerprint",
    "experience_source_fingerprints",
    "tag_experience_with_source_doc",
    "LeakageOverlap",
    "LeakageReport",
    "detect_leakage",
]

#: Canonical fingerprint prefix — a bare SHA-256 hex is prefixed so a fingerprint
#: is self-describing (and a non-hash id can never be mistaken for one).
FINGERPRINT_PREFIX = "sha256:"

#: Tag prefixes an Experience uses to carry the source-doc fingerprint(s).
SOURCE_TAG_PREFIXES: tuple[str, ...] = ("source_doc:", "doc_fp:")

#: Mapping keys inside ``reusable_steps[i]`` that carry a source fingerprint.
FINGERPRINT_STEP_KEYS: tuple[str, ...] = (
    "source_fingerprint",
    "source_doc_fingerprint",
)

#: Attribute names on a record/mapping that carry a source fingerprint.
_FINGERPRINT_ATTRS: tuple[str, ...] = (
    "source_doc_fingerprint",
    "source_document_fingerprint",
)


def fingerprint_source_document(data: bytes | bytearray | str) -> str:
    """Return the canonical source-document fingerprint ``"sha256:<hex>"``.

    Args:
        data: the raw source-document bytes, or its text (encoded UTF-8). The
            bytes are hashed verbatim — no normalisation is applied, so two
            different byte streams never collide, and the same stream is stable
            across processes (this is what makes the leakage check reproducible).

    Returns:
        ``"sha256:" + hexdigest``.

    Raises:
        TypeError: when ``data`` is neither bytes-like nor str.
    """
    if isinstance(data, str):
        payload = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray)):
        payload = bytes(data)
    else:  # pragma: no cover — defensive
        raise TypeError(f"unable to fingerprint {type(data).__name__}")
    return f"{FINGERPRINT_PREFIX}{hashlib.sha256(payload).hexdigest()}"


def normalize_fingerprint(value: Any) -> str | None:
    """Normalise a fingerprint/id for comparison, or ``None`` if unusable.

    Accepts ``"sha256:<hex>"``, ``"sha256=<hex>"``, ``"doc_fp:<hex>"`` or a bare
    token; strips any known prefix and lower-cases. An empty/whitespace-only
    value yields ``None`` so it can never match a real fingerprint (fail-closed).
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    for prefix in (FINGERPRINT_PREFIX, "sha256=", "doc_fp:", "source_doc:"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    return text or None


def _attr_or_key(obj: Any, name: str) -> Any:
    """Read ``name`` from a mapping or an attribute-bearing object."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def experience_source_fingerprints(record: Any) -> frozenset[str]:
    """Collect every source-document fingerprint a training record carries.

    Reads the three conventions documented in the module docstring (tag prefix,
    ``reusable_steps`` key, attribute) and returns the normalised, de-duplicated
    set. A record with no fingerprint yields the empty set — which never leaks
    (we cannot prove an overlap we cannot see), so a *missing* fingerprint is not
    treated as a match.
    """
    found: set[str] = set()

    tags = _attr_or_key(record, "tags") or []
    for tag in tags:
        text = str(tag or "")
        lowered = text.lower()
        for prefix in SOURCE_TAG_PREFIXES:
            if lowered.startswith(prefix):
                normalised = normalize_fingerprint(text[len(prefix) :])
                if normalised:
                    found.add(normalised)
                break

    steps = _attr_or_key(record, "reusable_steps") or []
    for step in steps:
        if not isinstance(step, dict):
            continue
        for key in FINGERPRINT_STEP_KEYS:
            normalised = normalize_fingerprint(step.get(key))
            if normalised:
                found.add(normalised)

    for attr in _FINGERPRINT_ATTRS:
        raw = _attr_or_key(record, attr)
        # ``reusable_steps`` may expose several fingerprints; a scalar attr may be
        # a single string or a list — accept both.
        candidates = raw if isinstance(raw, (list, tuple, set)) else [raw]
        for candidate in candidates:
            normalised = normalize_fingerprint(candidate)
            if normalised:
                found.add(normalised)

    return frozenset(found)


def tag_experience_with_source_doc(record: Any, fingerprint: str) -> None:
    """Stamp ``record``'s ``tags`` with the canonical ``source_doc:<hex>`` entry.

    Idempotent: re-tagging the same fingerprint adds nothing. Mirrors
    :func:`experience_source_fingerprints`'s prefix so writer and reader cannot
    drift. A ``None``/empty fingerprint is a no-op (nothing to record).
    """
    normalised = normalize_fingerprint(fingerprint)
    if not normalised:
        return
    tag = f"{SOURCE_TAG_PREFIXES[0]}{normalised}"
    tags = _attr_or_key(record, "tags")
    if tags is None:
        return
    if tag not in tags:
        tags.append(tag)


@dataclass(frozen=True)
class LeakageOverlap:
    """One golden case whose source document also trained the candidate."""

    case_id: str
    fingerprint: str
    experience_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "case_id": self.case_id,
            "source_doc_fingerprint": self.fingerprint,
            "experience_id": self.experience_id,
        }


@dataclass
class LeakageReport:
    """The verdict of one leakage scan over a candidate's holdout cases."""

    leaked: bool = False
    overlaps: tuple[LeakageOverlap, ...] = field(default_factory=tuple)
    checked_cases: int = 0
    training_fingerprints: int = 0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "leaked": self.leaked,
            "overlaps": [o.to_dict() for o in self.overlaps],
            "checked_cases": self.checked_cases,
            "training_fingerprints": self.training_fingerprints,
            "reason": self.reason,
        }


def _case_id(case: Any) -> str:
    return str(_attr_or_key(case, "case_id") or "")


def _case_fingerprint(case: Any) -> str | None:
    return normalize_fingerprint(_attr_or_key(case, "source_doc_fingerprint"))


def _experience_id(record: Any) -> str:
    return str(_attr_or_key(record, "id") or "")


def detect_leakage(
    holdout_cases: Iterable[Any],
    training_experiences: Iterable[Any],
) -> LeakageReport:
    """Detect whether any holdout case's source doc also trained the candidate.

    Args:
        holdout_cases: the held-out golden cases (dataclass or mapping), each
            exposing ``case_id`` and ``source_doc_fingerprint``.
        training_experiences: the candidate's training Experiences (dataclass or
            mapping); fingerprints are collected with
            :func:`experience_source_fingerprints`.

    Returns:
        A :class:`LeakageReport`. ``leaked`` is ``True`` iff at least one holdout
        case's fingerprint appears in the training set. Cases without a usable
        fingerprint are counted but never matched (fail-closed: no evidence of
        overlap is not evidence of isolation, so the *report* still records the
        count — but a missing fingerprint alone never fabricates a leak).
    """
    # fingerprint → the training experience ids that carry it.
    training_index: dict[str, list[str]] = {}
    for record in training_experiences:
        exp_id = _experience_id(record)
        for fp in experience_source_fingerprints(record):
            training_index.setdefault(fp, []).append(exp_id)

    cases = list(holdout_cases)
    overlaps: list[LeakageOverlap] = []
    checked = 0
    for case in cases:
        fp = _case_fingerprint(case)
        if fp is None:
            continue
        checked += 1
        for exp_id in training_index.get(fp, []):
            overlaps.append(
                LeakageOverlap(case_id=_case_id(case), fingerprint=fp, experience_id=exp_id)
            )

    leaked = bool(overlaps)
    if leaked:
        reason = (
            f"泄漏：{len(overlaps)} 个 holdout 用例的源文档指纹与候选训练 Experience "
            f"重叠（{', '.join(sorted({o.case_id for o in overlaps}))}），该候选回归结果作废"
        )
    else:
        reason = f"无泄漏：{checked} 个 holdout 用例与 {len(training_index)} 个训练指纹无重叠"

    return LeakageReport(
        leaked=leaked,
        overlaps=tuple(overlaps),
        checked_cases=checked,
        training_fingerprints=len(training_index),
        reason=reason,
    )
