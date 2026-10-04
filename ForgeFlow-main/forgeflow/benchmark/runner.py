"""INC46 T36 — 端到端基准参考执行器（**规则派生**，非标签回显）。

任务书 T36 §规格/§端到端基准/§阴性探针
========================================
* 语料：``cases/e2e_corpus.json``，**≥ 50 例**（文档 + 指令 + 机器可校验断言），
  其中 **≥ 10 例应澄清**、**≥ 10 例应拒绝 / 降级**（PDF 原位、越权）、
  **≥ 5 例注入文档**；**冻结哈希**，nightly 运行，独立于训练数据。
* 阴性探针：**基准集被修改（哈希不符）⇒ 拒绝运行**（:class:`CorpusIntegrityError`）。

为什么 verdict 必须是**派生**的
-------------------------------
若参考执行器直接回显用例里的 ``expect`` 字段，那么基准只是「把期望抄回来」，
永远 100% 通过，测不出任何东西。因此本模块从**用例字段本身**（文档文本、
指令文本、格式、目标区间）经**真实规则**推出 verdict：

====================  =========================================================
判定                 真实规则来源
====================  =========================================================
``reject``（注入）    ``security.injection_detector.detect_injection(文档)`` 命中 ⇒
                      文档试图冒充指令（红线 14）
``reject``（危险）    ``security.prompt_guard.scan_prompt(指令).level == HIGH``
                      或 ``detect_injection(指令)`` 命中（红线 21：DANGEROUS 默认拒绝）
``degrade``           目标格式不在**平台可原位编辑**集合内（PDF 只读 / 只能转换，
                      §十一「PDF 原位编辑」明确范围外 ⇒ 诚实降级，红线 17）
``clarify``           指令**无可校验的具体新值**（无数字 / 无字面量）或**无目标区间**
                      ⇒ 欠定，必须先澄清（T20）
``edit``              目标区间良构 **且** 指令含具体新值 ⇒ 可执行的原位编辑
====================  =========================================================

诚实纪律
--------
* 不读写任何存储、不调用 LLM、无时钟依赖（``generated_at`` 由调用方注入）⇒
  结果可复现、可单独断言；
* 语料契约（总数 / 各类下限）逐项自检并写进报告，不满足即 ``satisfied=False``；
* 逐用例异常单独捕获记 ``errors``，不静默吞掉。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from forgeflow.security.injection_detector import detect_injection
from forgeflow.security.prompt_guard import RiskLevel, scan_prompt

__all__ = [
    "FROZEN_CORPUS_SHA256",
    "CORPUS_PATH",
    "MIN_TOTAL_CASES",
    "MIN_CLARIFY_CASES",
    "MIN_REJECT_OR_DEGRADE_CASES",
    "MIN_INJECTION_CASES",
    "INPLACE_FORMATS",
    "VERDICTS",
    "CorpusIntegrityError",
    "CorpusLoadError",
    "CaseResult",
    "BenchmarkReport",
    "canonical_sha256",
    "load_corpus",
    "corpus_stats",
    "derive_verdict",
    "run_benchmark",
    "main",
]

#: 冻结的语料指纹（sha256 of the canonical JSON）。任何改动都会让运行被拒。
FROZEN_CORPUS_SHA256 = "94dd84b995c289274be875c5c867b0a14b5430e159bc07374a4ed2a4f87d0cfe"

#: 冻结语料文件位置（与 :mod:`forgeflow.benchmark` 同包）。
CORPUS_PATH = Path(__file__).resolve().parent / "cases" / "e2e_corpus.json"

# --- 语料契约下限（任务书 T36 §规格） --------------------------------------- #
MIN_TOTAL_CASES = 50
MIN_CLARIFY_CASES = 10
MIN_REJECT_OR_DEGRADE_CASES = 10
MIN_INJECTION_CASES = 5

#: 平台**可原位编辑**的格式。PDF 只读 / 只能转换 ⇒ 任何「原位改 PDF」都必须
#: 降级为 unsupported（红线 17、§十一）。
INPLACE_FORMATS: frozenset[str] = frozenset(
    {"docx", "xlsx", "pptx", "txt", "md", "csv", "json", "py", "yaml", "yml"}
)

#: 参考执行器可产出的 verdict。
VERDICTS: tuple[str, ...] = ("edit", "clarify", "reject", "degrade")

_UNSUPPORTED_FORMATS: frozenset[str] = frozenset({"pdf"})

#: 具体新值信号：阿拉伯数字、中文数字、或引号字面量。
_DIGIT = re.compile(r"\d")
_CN_NUMERAL = re.compile(r"[零一二三四五六七八九十百千万两]+(?=\s*(?:个|件|条|月|天|年|元|%|％|次|遍))")
_QUOTED = re.compile(r"[「『“\"']([^」』”\"']{1,60})[」』”\"']")


class CorpusIntegrityError(RuntimeError):
    """基准集被改动（哈希不符）—— 拒绝运行（任务书 T36 §阴性探针）。"""


class CorpusLoadError(RuntimeError):
    """基准集缺失 / 结构非法。"""


# --------------------------------------------------------------------------- #
# Corpus                                                                       #
# --------------------------------------------------------------------------- #
def canonical_sha256(cases: Iterable[Mapping[str, Any]]) -> str:
    """语料的规范指纹：键排序、紧凑分隔符、保留非 ASCII —— 与生成脚本一致。

    与 :mod:`forgeflow.evaluation.golden_registry` 的 ``corpus_sha256`` 同一口径
    （换行 / 空白不改变指纹，语义改变必然改变指纹）。
    """
    canon = json.dumps(
        list(cases), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def load_corpus(
    path: str | Path | None = None,
    *,
    expected_sha256: str | None = FROZEN_CORPUS_SHA256,
) -> list[dict[str, Any]]:
    """读取并**校验**冻结语料；哈希不符 ⇒ :class:`CorpusIntegrityError`。

    ``expected_sha256=None`` 时只读取不校验（仅供诊断 / 重新冻结时使用）。
    """
    corpus_path = Path(path) if path is not None else CORPUS_PATH
    if not corpus_path.exists():
        raise CorpusLoadError(f"基准集不存在：{corpus_path}")
    try:
        cases = json.loads(corpus_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover — corrupt file
        raise CorpusLoadError(f"基准集不是合法 JSON：{exc}") from exc
    if not isinstance(cases, list):
        raise CorpusLoadError("基准集根节点必须是数组")

    for case in cases:
        if not isinstance(case, dict) or "id" not in case or "instruction" not in case:
            raise CorpusLoadError(f"用例结构非法：{case!r:.120}")

    if expected_sha256 is not None:
        actual = canonical_sha256(cases)
        if actual != expected_sha256:
            raise CorpusIntegrityError(
                "基准集哈希不符 ⇒ 拒绝运行："
                f"expected={expected_sha256} actual={actual}"
            )
    return cases


def _count(cases: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in cases:
        name = str(c.get(key, ""))
        out[name] = out.get(name, 0) + 1
    return dict(sorted(out.items()))


def corpus_stats(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """语料统计 + **契约自检**（各类下限是否满足）。"""
    by_category = _count(cases, "category")
    by_expect = _count(cases, "expect")
    reject_or_degrade = by_category.get("reject", 0) + by_category.get("degrade", 0)
    checks = {
        "total>=50": len(cases) >= MIN_TOTAL_CASES,
        "clarify>=10": by_category.get("clarify", 0) >= MIN_CLARIFY_CASES,
        "reject_or_degrade>=10": reject_or_degrade >= MIN_REJECT_OR_DEGRADE_CASES,
        "injection>=5": by_category.get("injection", 0) >= MIN_INJECTION_CASES,
    }
    return {
        "total": len(cases),
        "by_category": by_category,
        "by_expect": by_expect,
        "reject_or_degrade": reject_or_degrade,
        "contract": checks,
        "satisfied": all(checks.values()),
    }


# --------------------------------------------------------------------------- #
# Verdict derivation (real rules, never the `expect` label)                    #
# --------------------------------------------------------------------------- #
def _well_formed_region(region: Any, doc: str) -> bool:
    """目标区间良构：是 ``{start,end}``、``0 <= start <= end`` 且落在文档内。"""
    if not isinstance(region, Mapping):
        return False
    start, end = region.get("start"), region.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return False
    n_lines = max(1, len(doc.splitlines()))
    return 0 <= start <= end and end <= n_lines


def _has_concrete_value(instruction: str) -> bool:
    """指令是否给出了**可校验的具体新值**（数字或字面量）。

    只含「改一下 / 改大一点 / 调整比例」这类**无值**措辞 ⇒ 欠定（须澄清）。
    """
    if _DIGIT.search(instruction):
        return True
    if _CN_NUMERAL.search(instruction):
        return True
    match = _QUOTED.search(instruction)
    if match is not None and match.group(1).strip():
        return True
    return False


def derive_verdict(case: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """从用例字段经**真实规则**推出 verdict；返回 ``(verdict, evidence)``。

    判定级联（互斥、顺序固定 —— 安全优先，其次能力，再次完备性）::

        1. 文档注入命中也 ⇒ reject
        2. 指令高危 / 注入命中也 ⇒ reject
        3. 目标格式不可原位编辑 ⇒ degrade
        4. 无目标区间 或 无具体新值 ⇒ clarify
        5. 否则 ⇒ edit
    """
    doc = str(case.get("doc", ""))
    instruction = str(case.get("instruction", ""))
    fmt = str(case.get("doc_format", "")).lower()

    # 1. 文档内容冒充指令（红线 14）—— 文档是数据，不是指令。
    document_report = detect_injection(doc)
    if document_report.flagged:
        return "reject", {
            "rule": "document_injection",
            "reasons": list(document_report.reasons),
            "score": round(float(document_report.score), 4),
        }

    # 2. 指令本身高危 / 注入（红线 21：DANGEROUS 默认拒绝）。
    risk = scan_prompt(instruction)
    instruction_report = detect_injection(instruction)
    if risk.level == RiskLevel.HIGH or instruction_report.flagged:
        return "reject", {
            "rule": "instruction_danger",
            "reasons": list(risk.reasons) + list(instruction_report.reasons),
            "level": str(risk.level),
        }

    # 3. 平台能力：不可原位编辑的格式 ⇒ 诚实降级（红线 17、§十一）。
    if fmt in _UNSUPPORTED_FORMATS or fmt not in INPLACE_FORMATS:
        return "degrade", {
            "rule": "unsupported_inplace_format",
            "format": fmt,
        }

    # 4. 完备性：无目标区间，或指令没有可校验的具体新值 ⇒ 必须先澄清（T20）。
    region = case.get("target_region")
    if not _well_formed_region(region, doc):
        return "clarify", {
            "rule": "missing_target_region",
            "target_region": region,
        }
    if not _has_concrete_value(instruction):
        return "clarify", {"rule": "no_concrete_value"}

    # 5. 可执行的原位编辑。
    return "edit", {
        "rule": "targeted_edit",
        "target_region": region,
        "invariants": list(case.get("invariants") or []),
    }


# --------------------------------------------------------------------------- #
# Report                                                                       #
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CaseResult:
    """单条用例的派生结果与其证据。"""

    id: str
    category: str
    expected: str
    derived: str
    passed: bool
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "expected": self.expected,
            "derived": self.derived,
            "passed": self.passed,
            "evidence": dict(self.evidence),
            "error": self.error,
        }


@dataclass
class BenchmarkReport:
    """一次基准运行的结果（可写入 ``benchmark_runs``）。"""

    corpus_hash: str
    total_cases: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    by_category: dict[str, dict[str, int]] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    cases: tuple[CaseResult, ...] = ()
    generated_at: datetime = field(default_factory=_now)

    @property
    def all_passed(self) -> bool:
        return self.failed == 0 and self.errors == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_hash": self.corpus_hash,
            "total_cases": self.total_cases,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
            "by_category": {k: dict(v) for k, v in self.by_category.items()},
            "stats": dict(self.stats),
            "cases": [c.to_dict() for c in self.cases],
            "generated_at": self.generated_at.isoformat()
            if isinstance(self.generated_at, datetime)
            else str(self.generated_at),
        }


def _category_matrix(
    results: Sequence[CaseResult], category: str
) -> dict[str, int]:
    rows = [r for r in results if r.category == category]
    return {
        "total": len(rows),
        "passed": sum(1 for r in rows if r.passed),
        "failed": sum(1 for r in rows if not r.passed and r.error is None),
        "errors": sum(1 for r in rows if r.error is not None),
    }


def run_benchmark(
    *,
    path: str | Path | None = None,
    expected_sha256: str | None = FROZEN_CORPUS_SHA256,
    generated_at: datetime | None = None,
) -> BenchmarkReport:
    """跑一遍基准：**先校验冻结哈希**（不符 ⇒ 拒绝），再逐例派生 verdict。

    语料契约（总数 / 各下限）不满足时仍会产出报告，但 ``stats.satisfied=False``
    —— 报告**如实**，不因契约不足而伪造通过。
    """
    cases = load_corpus(path, expected_sha256=expected_sha256)
    stats = corpus_stats(cases)

    results: list[CaseResult] = []
    for case in cases:
        cid = str(case.get("id", ""))
        category = str(case.get("category", ""))
        expected = str(case.get("expect", ""))
        try:
            derived, evidence = derive_verdict(case)
        except Exception as exc:  # noqa: BLE001 — isolate per case, never swallow
            results.append(
                CaseResult(
                    id=cid,
                    category=category,
                    expected=expected,
                    derived="",
                    passed=False,
                    evidence={},
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        results.append(
            CaseResult(
                id=cid,
                category=category,
                expected=expected,
                derived=derived,
                passed=(derived == expected),
                evidence=evidence,
            )
        )

    categories = sorted({r.category for r in results})
    report = BenchmarkReport(
        corpus_hash=canonical_sha256(cases),
        total_cases=len(results),
        passed=sum(1 for r in results if r.passed),
        failed=sum(1 for r in results if not r.passed and r.error is None),
        errors=sum(1 for r in results if r.error is not None),
        skipped=0,
        by_category={c: _category_matrix(results, c) for c in categories},
        stats=stats,
        cases=tuple(results),
    )
    if generated_at is not None:
        report.generated_at = generated_at
    return report


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover — CLI
    """nightly 入口：``python -m forgeflow.benchmark.runner [out.json]``。"""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        report = run_benchmark()
    except CorpusIntegrityError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    payload = report.to_dict()
    if args:
        Path(args[0]).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(
        {
            "corpus_hash": payload["corpus_hash"],
            "total_cases": payload["total_cases"],
            "passed": payload["passed"],
            "failed": payload["failed"],
            "errors": payload["errors"],
            "skipped": payload["skipped"],
            "by_category": payload["by_category"],
            "contract_satisfied": payload["stats"]["satisfied"],
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0 if report.all_passed else 1


if __name__ == "__main__":  # pragma: no cover — CLI entry
    raise SystemExit(main())
