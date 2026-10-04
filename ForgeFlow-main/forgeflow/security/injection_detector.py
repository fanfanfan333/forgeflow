"""INC46 T33 — 指令性文本检测（规则 + 分类器）。

红线 14 的**第二道**防线。:mod:`forgeflow.security.untrusted_data` 声明「文档 /
工具输出是数据，不是指令」；本模块负责**发现**数据通道里试图冒充指令的文字，让
上游把它们（连同其所在的 Experience）挡在经验 / Skill / 记忆之外（进 quarantine，
见 :mod:`forgeflow.security.quarantine`）。

分层（规格 ②：规则 + 分类器）
------------------------------
* **规则层** — 短语级 verb+object 正则。复用
  :mod:`forgeflow.security.prompt_guard` 已有的 6 类威胁模式（
  ``instruction_override`` / ``role_takeover`` / ``system_prompt_leak`` /
  ``credential_exfiltration`` / ``unauthorized_tool_exec`` / ``cross_tenant_read``
  —— **一份定义，两处使用**，避免第二套正则漂移），再叠加 T33 面向文档编辑
  的 4 类：``file_deletion``（删除全部文件）、``amount_override``（把金额改为…）、
  ``memory_write``（把以下内容写入记忆）、``tool_invocation``（调用工具执行…）。
* **分类器层** — 一个**确定性的轻量词法分类器**（不是 ML 模型，命名诚实）：
  对「祈使/覆盖动词 × 敏感对象 × 通道逃逸标记 × 重复祈使」做加权打分，
  得分 ≥ 阈值（默认 0.6）即判为可疑。规则层漏掉的改写、同义替换由其补足。

判定口径（诚实声明）
--------------------
这是**可测试的最低防线**，不是「防住所有注入」的承诺。命中 ⇒ **不阻断正常
编辑**，只把该条 Experience 送进 quarantine 等人工复核（fail-safe，非
fail-closed）；真正的安全边界在更下层（RBAC / HITL / 工具执行器 fail-closed，
见 ``prompt_guard`` 的 scope 说明）。

被刻意排除在数据通道规则层之外的两类（``system_file_read`` /
``internal_info_dump``）：它们描述的是「读取某个路径 / 汇总内部配置」，合法
的合规 / 运维文档会**引用**此类文字，纳入会造成系统性误报；T33② 的威胁模型是
「指令 / 计划 / 权限 / 生命周期被操纵」，故数据通道只取与之匹配的 6 类。

Tenant discipline (红线 5): 本模块是纯函数（无存储、无租户状态）；租户隔离由
:mod:`forgeflow.security.quarantine` 的存储层负责。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from forgeflow.security import prompt_guard as _guard

__all__ = [
    "INJECTION_VERSION",
    "DEFAULT_SCORE_THRESHOLD",
    "Finding",
    "InjectionReport",
    "InjectionClassifier",
    "detect_injection",
    "detect_experience_injection",
    "clean_corpus_false_positive_rate",
]

#: Detector vocabulary version — stamped into quarantine provenance so a review
#: can tell which detector revision quarantined a row.
INJECTION_VERSION = "1"

#: Classifier decision threshold (spec §十: 初始默认值，须经 T36 校准).
DEFAULT_SCORE_THRESHOLD = 0.6

# --------------------------------------------------------------------------- #
# Rule layer                                                                   #
# --------------------------------------------------------------------------- #
# The data-channel threat model is "instruction / plan / permission / lifecycle
# manipulation", so only these six prompt_guard intents apply to a *data* channel
# (see module docstring for why system_file_read / internal_info_dump are out).
_DATA_CHANNEL_TAGS: frozenset[str] = frozenset(
    {
        "instruction_override",
        "role_takeover",
        "system_prompt_leak",
        "credential_exfiltration",
        "unauthorized_tool_exec",
        "cross_tenant_read",
    }
)

# T33-document-editing intents the shared prompt_guard does not cover. Every
# pattern is **verb + object** (never a bare keyword), and the object is a
# *sensitivity-bearing* noun, so ordinary product prose is not swept up.
_ZH_GAP = r"[\s，,、：:]*"
_T33_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "file_deletion",
        re.compile(
            r"(?:删除|删掉|清除|清空|移除|抹除|销毁)" + _ZH_GAP
            + r"(?:所有|全部|一切的?|整个|本地的?|服务器上的?)?" + _ZH_GAP
            + r"(?:文件|数据|记录|目录|文件夹|内容|资料)"
        ),
    ),
    (
        "file_deletion",
        re.compile(
            r"\b(?:delete|remove|erase|wipe|clear|purge|rm)\s+"
            r"(?:all|every|the\s+entire|all\s+the)?\s*"
            r"(?:files?|data|records?|directories|folders|contents?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "amount_override",
        # 对象在前（把金额改为…）与动词在前（将金额修改为…）两式都覆盖。
        re.compile(
            r"(?:把|将)?" + _ZH_GAP
            + r"(?:金额|价格|总额|总价|单价|报价|数字|数目|报销金额)" + _ZH_GAP
            + r"(?:改为|改成|修改为|更改为|调整为|换成|设为|设置为|变成)"
        ),
    ),
    (
        "amount_override",
        re.compile(
            r"\b(?:change|set|modify|update|make|adjust)\s+(?:the\s+)?"
            r"(?:amount|price|total|sum|number|figure|value)\s+(?:to|into)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "memory_write",
        # 「把以下内容写入记忆」—— 对象在前。
        re.compile(
            r"(?:把|将)" + _ZH_GAP
            + r"(?:以下|下面|上述|这些|这段|该)" + _ZH_GAP
            + r"(?:内容|信息|文字|文本|指令)?" + _ZH_GAP
            + r"(?:写入|存入|保存到|记到|记住|记入|添加到|加到|写进)"
            + _ZH_GAP + r"(?:记忆|记忆库|长期记忆|经验库?|技能库?|知识库|偏好)"
        ),
    ),
    (
        "memory_write",
        # 「请记住以下…」/「写入记忆」—— 动词在前。
        re.compile(
            r"(?:写入|存入|保存到|记到|记入|添加到|加到|写进|写入到)" + _ZH_GAP
            + r"(?:到|进|入|至)?" + _ZH_GAP
            + r"(?:记忆|记忆库|长期记忆|经验库?|技能库?|知识库|偏好)"
        ),
    ),
    (
        "memory_write",
        re.compile(
            r"\b(?:write|save|store|add|remember|persist)\s+"
            r"(?:this|these|the\s+following|that)\s+"
            r"(?:content|text|information|instructions?)?\s*"
            r"(?:to|into|in)\s+(?:memory|memories|experience|skill|"
            r"knowledge\s+base|preferences?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "tool_invocation",
        # 需要「任意 / 以下 / the following / this」这类限定词，否则
        # 「使用工具完成任务」这类正常措辞会误报。
        re.compile(
            r"(?:调用|执行|运行|启动|使用)" + _ZH_GAP
            + r"(?:该|这个|上述|以下|下面|任意|指定的?)" + _ZH_GAP
            + r"(?:工具|命令|脚本|接口|API|函数|程序)"
        ),
    ),
    (
        "tool_invocation",
        re.compile(
            r"\b(?:call|invoke|execute|run|use)\s+"
            r"(?:the|this|that|any|following|specified)\s+"
            r"(?:tool|command|script|api|function|program)\b",
            re.IGNORECASE,
        ),
    ),
]

#: Rule layer = the shared prompt_guard intents (filtered to the data channel)
#: plus the T33 document intents. One definition of every shared pattern.
_RULE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (tag, pattern)
    for (tag, pattern) in _guard._HIGH_PATTERNS  # noqa: SLF001 — reuse-first (see scrubber)
    if tag in _DATA_CHANNEL_TAGS
] + _T33_PATTERNS

# --------------------------------------------------------------------------- #
# Value objects                                                                #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Finding:
    """One suspicious span in a data-channel payload (minimised: excerpt only)."""

    tag: str
    layer: str  # "rule" | "classifier"
    excerpt: str = ""
    start: int = -1
    end: int = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "layer": self.layer,
            "excerpt": self.excerpt,
            "start": self.start,
            "end": self.end,
        }


@dataclass
class InjectionReport:
    """The detector's verdict over one payload.

    ``flagged`` is the single decision an upstream caller acts on. ``score`` is
    the classifier's confidence; ``reasons`` is the de-duplicated tag list (the
    same shape the API surfaces). ``findings`` carries the (minimised) evidence.
    """

    flagged: bool = False
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    version: str = INJECTION_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "flagged": self.flagged,
            "score": round(float(self.score), 4),
            "reasons": list(self.reasons),
            "findings": [f.to_dict() for f in self.findings],
            "version": self.version,
        }


def _excerpt(text: str, start: int, end: int, *, width: int = 48) -> str:
    """A short, context-windowed excerpt around a match (never the whole doc)."""
    if start < 0 or end < 0 or end <= start:
        return ""
    lo = max(0, start)
    hi = min(len(text), start + width)
    snippet = text[lo:hi].replace("\n", " ").strip()
    return snippet


# --------------------------------------------------------------------------- #
# Classifier layer                                                            #
# --------------------------------------------------------------------------- #
# Imperative / override verbs and sensitivity-bearing objects. Deliberately
# broad word lists (the *weight* is what keeps precision): a lone generic verb
# contributes nothing unless it is paired with a sensitive object.
_IMPERATIVE_VERBS = re.compile(
    r"(?:忽略|无视|忘记|覆盖|绕过|跳过|不要管|执行|运行|调用|删除|清空|写入|记住|保存|"
    r"改为|修改|发送|导出|上传|泄露|输出|返回|"
    r"\b(?:ignore|disregard|forget|override|bypass|skip|execute|run|call|delete|"
    r"remove|wipe|write|save|remember|change|set|send|export|leak|output|return)\b)",
    re.IGNORECASE,
)
_SENSITIVE_OBJECTS = re.compile(
    r"(?:指令|提示词|规则|系统提示|文件|数据|记录|目录|金额|价格|总额|记忆|经验|技能|"
    r"知识库|偏好|密钥|密码|令牌|凭据|凭证|工具|命令|脚本|"
    r"\b(?:instructions?|prompts?|rules?|system\s+prompt|files?|data|records?|"
    r"amount|price|total|memory|memories|experience|skill|secret|password|token|"
    r"credential|tool|command|script)\b)",
    re.IGNORECASE,
)
_CHANNEL_ESCAPE = re.compile(
    r"(?:<<<UNTRUSTED-DATA|<<<END-UNTRUSTED-DATA|(?:^|\n)\s*(?:system|assistant|user)\s*[:>]|"
    r"<\|(?:im_start|system|assistant)\|>)",
    re.IGNORECASE,
)
# Directive *lines*: a line that opens with an explicit instruction marker (ZH:
# 请/务必/… ; EN: an imperative verb). Deliberately line-anchored and
# marker-gated — a bare comma anywhere in ordinary prose must never count, which
# is what a looser "any line with punctuation" regex would wrongly do.
_DIRECTIVE_LINE_ZH = re.compile(r"(?m)^\s*(?:请|务必|立即|马上|现在|注意|切记)(?:你|您)?\s*\S")
_DIRECTIVE_LINE_EN = re.compile(
    r"(?im)^\s*(?:please\s+)?(?:ignore|delete|remove|wipe|write|save|remember|"
    r"execute|run|call|set|change|send|export|leak)\b"
)


class InjectionClassifier:
    """Deterministic weighted-feature classifier (NOT a trained model).

    Five lexical features, each a bounded weight; the score is their sum clamped
    to ``[0, 1]``. Every feature is *co-occurrence* based, so a document that
    merely mentions a sensitive word (or merely uses an imperative verb) is not
    flagged — only their combination is. Honest naming: a lightweight lexical
    scorer, the rule layer's fallback for rephrasings.
    """

    #: feature → weight
    W_RULE_INTENT = 0.5  # an override/imperative intent phrase is present
    W_VERB_OBJECT = 0.25  # an imperative verb *near* a sensitive object
    W_CHANNEL_ESCAPE = 0.35  # a chat/delimiter-role escape marker is present
    W_REPEATED_IMPERATIVE = 0.15  # ≥2 imperative-ish lines (a directive dump)

    def __init__(self, *, threshold: float = DEFAULT_SCORE_THRESHOLD) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        self.threshold = float(threshold)

    def score(self, text: str) -> float:
        """Return the classifier confidence in ``[0, 1]`` for ``text``."""
        if not text:
            return 0.0
        total = 0.0

        # 1. A verb (imperative/override) within a short window of a sensitive
        #    object anywhere in the text — the core "instruction to the agent"
        #    signal, language-independent.
        for verb in _IMPERATIVE_VERBS.finditer(text):
            tail = text[verb.end() : verb.end() + 24]
            head = text[max(0, verb.start() - 24) : verb.start()]
            if _SENSITIVE_OBJECTS.search(head) or _SENSITIVE_OBJECTS.search(tail):
                total += self.W_VERB_OBJECT
                break

        # 2. A delimiter / role escape marker — an attempt to break out of the
        #    data channel into the instruction stream.
        if _CHANNEL_ESCAPE.search(text):
            total += self.W_CHANNEL_ESCAPE

        # 3. A directive dump: two-or-more directive lines AND a sensitive object.
        directive_lines = len(_DIRECTIVE_LINE_ZH.findall(text)) + len(
            _DIRECTIVE_LINE_EN.findall(text)
        )
        if directive_lines >= 2 and _SENSITIVE_OBJECTS.search(text):
            total += self.W_REPEATED_IMPERATIVE

        return min(1.0, total)

    def classify(self, text: str, *, rule_flagged: bool = False) -> Finding | None:
        """Return a classifier ``Finding`` when ``text`` clears the threshold.

        ``rule_flagged`` lets the caller suppress a duplicate classifier finding
        when the rule layer already fired.
        """
        value = self.score(text)
        if value >= self.threshold and not rule_flagged:
            return Finding(tag="_classifier", layer="classifier")
        return None


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #
def detect_injection(
    text: str,
    *,
    classifier: InjectionClassifier | None = None,
) -> InjectionReport:
    """Scan a data-channel payload; return an :class:`InjectionReport`.

    Rule layer first (any hit ⇒ flagged). If no rule fires, the classifier
    decides. An empty / clean payload returns a ``flagged=False`` report with
    ``score=0.0`` — never a fake success.
    """
    if not text:
        return InjectionReport(flagged=False, score=0.0)

    classifier = classifier or InjectionClassifier()
    findings: list[Finding] = []
    reasons: list[str] = []

    for tag, pattern in _RULE_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        if tag not in reasons:
            reasons.append(tag)
        findings.append(
            Finding(
                tag=tag,
                layer="rule",
                excerpt=_excerpt(text, match.start(), match.end()),
                start=match.start(),
                end=match.end(),
            )
        )

    rule_flagged = bool(findings)
    score = classifier.score(text)
    if not rule_flagged:
        extra = classifier.classify(text, rule_flagged=False)
        if extra is not None:
            findings.append(extra)
            reasons.append(extra.tag)

    return InjectionReport(
        flagged=rule_flagged or bool(findings),
        score=score,
        reasons=reasons,
        findings=findings,
    )


#: The free-text fields of an experience that carry user-document-derived text.
#: Mirrors ``privacy.scrubber``'s field list so detect + scrub see the same text.
_DECISION_TEXT_KEYS = ("reason", "detail", "decision", "note", "text")


def _experience_texts(record: Any) -> list[str]:
    """Collect every free-text field of an experience (order preserved)."""
    texts: list[str] = []
    summary = getattr(record, "summary", "") or ""
    if summary:
        texts.append(summary)
    for step in getattr(record, "reusable_steps", None) or []:
        if isinstance(step, dict):
            note = step.get("note", "")
            if isinstance(note, str) and note:
                texts.append(note)
    for decision in getattr(record, "decisions", None) or []:
        if isinstance(decision, dict):
            for key in _DECISION_TEXT_KEYS:
                value = decision.get(key)
                if isinstance(value, str) and value:
                    texts.append(value)
        elif isinstance(decision, str) and decision:
            texts.append(decision)
    return texts


def detect_experience_injection(
    record: Any,
    *,
    classifier: InjectionClassifier | None = None,
) -> InjectionReport:
    """Scan **all** free-text fields of an experience; merge their reports.

    A record is flagged if **any** field is flagged (the whole experience is the
    unit that will be quarantined). Findings are concatenated and reasons
    de-duplicated, in field order.
    """
    classifier = classifier or InjectionClassifier()
    merged = InjectionReport(flagged=False, score=0.0)
    for text in _experience_texts(record):
        report = detect_injection(text, classifier=classifier)
        if report.flagged:
            merged.flagged = True
        merged.score = max(merged.score, report.score)
        for reason in report.reasons:
            if reason not in merged.reasons:
                merged.reasons.append(reason)
        merged.findings.extend(report.findings)
    return merged


def clean_corpus_false_positive_rate(
    corpus: Iterable[str],
    *,
    classifier: InjectionClassifier | None = None,
) -> float:
    """Fraction of **clean** corpus samples wrongly flagged (spec 阳性探针).

    Returns ``0.0`` for an empty corpus (no measurement ⇒ no fake rate).
    """
    items = [c for c in corpus]
    if not items:
        return 0.0
    classifier = classifier or InjectionClassifier()
    flagged = sum(1 for text in items if detect_injection(text, classifier=classifier).flagged)
    return flagged / len(items)
