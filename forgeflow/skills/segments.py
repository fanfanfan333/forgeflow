"""INC46 T07 — the seven-segment contract: segment registry + pure helpers.

Why this module
---------------
A ForgeFlow skill contract has exactly seven segments (七段契约). The segment
**names** and their SKILL.md destinations are the authoritative mapping owned by
T18 (:mod:`forgeflow.skills.spec_mapping`, ``SEVEN_SECTIONS`` /
``SECTION_MAPPING``). This module adds the pieces T07 needs on top of that
mapping, keeping a single source of truth:

* the human-facing segment titles (for actionable error messages),
* the required/optional policy for the T07 contract (all seven are required —
  a missing segment is an explicit error, never a silent completion), and
* dependency-free helpers both the schema layer and the completion layer use:
  segment-key normalisation, body volume findings, and reference-depth findings.

It deliberately holds **no** pydantic models and performs **no** I/O, so
:mod:`forgeflow.skills.schemas` (the models) and
:mod:`forgeflow.skills.contract_completion` (the completion layer) can both
import it without forming a cycle.

Relationship to the existing contract layer
-------------------------------------------
* :class:`forgeflow.skills.draft_spec.DraftSpec` — the compiler's four-element
  draft (prompt/steps/tools/io_schema). It cannot express the spec-only segments
  (Knowledge / Evaluation / Examples); nothing here fabricates them.
* :class:`forgeflow.skills.contracts.SkillContract` — the INC43 dataclass the
  engineering loop reviews in. Untouched.
* :class:`forgeflow.skills.schemas.SkillContractDocument` — the T07 Pydantic
  seven-segment contract, aligned to the SKILL.md specification (T18).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from forgeflow.skills.spec_mapping import SEVEN_SECTIONS
from forgeflow.skills.spec_validator import (
    BODY_LINE_RECOMMENDED_MAX,
    BODY_TOKEN_RECOMMENDED_MAX,
)

__all__ = [
    "SEGMENT_MANIFEST",
    "SEGMENT_KNOWLEDGE",
    "SEGMENT_PROCEDURE",
    "SEGMENT_POLICIES",
    "SEGMENT_TOOL_BINDINGS",
    "SEGMENT_EVALUATION",
    "SEGMENT_EXAMPLES",
    "SEGMENT_ORDER",
    "SEGMENT_TITLES",
    "REQUIRED_SEGMENTS",
    "OPTIONAL_SEGMENTS",
    "normalise_segment_key",
    "segment_title",
    "missing_segments",
    "estimate_tokens",
    "body_volume_findings",
    "normalize_reference",
    "is_reference_too_deep",
    "reference_depth_findings",
    "contract_body_text",
]

# --------------------------------------------------------------------------- #
# segment identity (names/order are the T18 mapping's; titles are T07's)      #
# --------------------------------------------------------------------------- #
SEGMENT_MANIFEST = "manifest"
SEGMENT_KNOWLEDGE = "knowledge"
SEGMENT_PROCEDURE = "procedure"
SEGMENT_POLICIES = "policies"
SEGMENT_TOOL_BINDINGS = "tool_bindings"
SEGMENT_EVALUATION = "evaluation"
SEGMENT_EXAMPLES = "examples"

#: Canonical segment order — the T18 seven-section order, reused verbatim so the
#: contract and the SKILL.md mapping can never drift apart.
SEGMENT_ORDER: tuple[str, ...] = tuple(SEVEN_SECTIONS)

#: Human-facing titles used in error messages (段名固定，勿改名).
SEGMENT_TITLES: dict[str, str] = {
    SEGMENT_MANIFEST: "Manifest（身份 / 元数据）",
    SEGMENT_KNOWLEDGE: "Knowledge（知识）",
    SEGMENT_PROCEDURE: "Procedure（过程步骤）",
    SEGMENT_POLICIES: "Policies（约束 / 工具白名单声明）",
    SEGMENT_TOOL_BINDINGS: "Tool bindings（工具绑定）",
    SEGMENT_EVALUATION: "Evaluation（评估）",
    SEGMENT_EXAMPLES: "Examples（示例）",
}

#: T07 rule: **all seven** segments are required. A missing segment is an
#: explicit, named error — it is never silently completed (含 Knowledge / Examples)。
REQUIRED_SEGMENTS: tuple[str, ...] = tuple(SEGMENT_ORDER)

#: No segment is optional under the T07 contract; kept explicit so the
#: required/optional split is reviewable in one place (and so the counterfactual
#: "make a segment optional" has a single, obvious edit site).
OPTIONAL_SEGMENTS: tuple[str, ...] = ()

#: Tolerated spellings for a segment key → the canonical key. ``tool bindings``
#: is the human name from the task book; YAML/JSON authors also write
#: ``tool-bindings``. Every form collapses onto the canonical key above.
_SEGMENT_ALIASES: dict[str, str] = {
    "manifest": SEGMENT_MANIFEST,
    "meta": SEGMENT_MANIFEST,
    "metadata": SEGMENT_MANIFEST,
    "knowledge": SEGMENT_KNOWLEDGE,
    "procedure": SEGMENT_PROCEDURE,
    "steps": SEGMENT_PROCEDURE,
    "policies": SEGMENT_POLICIES,
    "policy": SEGMENT_POLICIES,
    "tool_bindings": SEGMENT_TOOL_BINDINGS,
    "tool_binding": SEGMENT_TOOL_BINDINGS,
    "toolbindings": SEGMENT_TOOL_BINDINGS,
    "tool_bindings_": SEGMENT_TOOL_BINDINGS,
    "evaluation": SEGMENT_EVALUATION,
    "eval": SEGMENT_EVALUATION,
    "examples": SEGMENT_EXAMPLES,
    "example": SEGMENT_EXAMPLES,
}


def normalise_segment_key(key: Any) -> str:
    """Map any tolerated segment spelling onto its canonical key.

    ``"Tool bindings"`` / ``"tool-bindings"`` / ``"tool_bindings"`` all become
    ``"tool_bindings"``. An unknown key is returned unchanged (lowercased, with
    dashes/spaces folded to underscores) so the caller can report it verbatim.
    """
    raw = str(key or "").strip()
    folded = re.sub(r"[\s-]+", "_", raw.strip().lower())
    if folded in _SEGMENT_ALIASES:
        return _SEGMENT_ALIASES[folded]
    return _SEGMENT_ALIASES.get(raw.strip().lower(), folded)


def segment_title(name: Any) -> str:
    """The Chinese title for a segment, or the raw key when it is unknown."""
    canonical = normalise_segment_key(name)
    return SEGMENT_TITLES.get(canonical, str(name))


def missing_segments(present_keys: Iterable[Any]) -> list[str]:
    """Required segments absent from ``present_keys`` (canonical keys, in order)."""
    present = {normalise_segment_key(k) for k in present_keys}
    return [segment for segment in REQUIRED_SEGMENTS if segment not in present]


# --------------------------------------------------------------------------- #
# progressive-disclosure findings (advisory only; T07 emits the signal)        #
# --------------------------------------------------------------------------- #
def estimate_tokens(text: str) -> int:
    """Rough token estimate (≈4 chars/token) — the same heuristic T18 reports.

    Reported *as* an estimate; it never truncates and never gates.
    """
    return len(text or "") // 4


def body_volume_findings(body: str) -> list[str]:
    """Advisory findings when the body exceeds the recommended size.

    Thresholds are reused verbatim from T18's :mod:`spec_validator`
    (``BODY_LINE_RECOMMENDED_MAX`` / ``BODY_TOKEN_RECOMMENDED_MAX``). Exceeding
    them is a **finding**, never an error and never a silent truncation — the
    task-book bodies are always returned whole (T08 consumes this signal).
    """
    findings: list[str] = []
    line_count = len(body.splitlines()) if body.strip() else 0
    if line_count > BODY_LINE_RECOMMENDED_MAX:
        findings.append(
            f"正文 {line_count} 行，超过建议的 {BODY_LINE_RECOMMENDED_MAX} 行上限"
            "（渐进披露：详细内容应移至 references/；本层只提示、不截断）"
        )
    token_estimate = estimate_tokens(body)
    if token_estimate > BODY_TOKEN_RECOMMENDED_MAX:
        findings.append(
            f"正文约 {token_estimate} tokens（粗估），超过建议的 "
            f"{BODY_TOKEN_RECOMMENDED_MAX} tokens（渐进披露：拆分至 references/）"
        )
    return findings


#: A reference file path: ``scripts/`` / ``references/`` / ``assets/`` + a tail.
_REFERENCE_RE = re.compile(r"(?:scripts|references|assets)/[^\s)\]\"']+")


def normalize_reference(ref: Any, *, default_prefix: str = "references/") -> str:
    """Return a directory-relative reference, prefixing a bare filename.

    ``"examples.md"`` → ``"references/examples.md"``; an already-prefixed path
    (``"scripts/run.py"``) is returned unchanged. An empty value stays empty.
    """
    text = str(ref or "").strip()
    if not text:
        return ""
    if not _REFERENCE_RE.match(text):
        return f"{default_prefix}{text}"
    return text


def is_reference_too_deep(ref: Any) -> bool:
    """True when a ``scripts/`` / ``references/`` / ``assets/`` ref nests ≥2 deep.

    Mirrors T18's rule: file references must stay one level below ``SKILL.md``.
    """
    text = str(ref or "").strip()
    match = _REFERENCE_RE.match(text)
    if not match:
        return False
    remainder = text.split("/", 1)[1]
    return "/" in remainder


def reference_depth_findings(refs: Iterable[Any]) -> list[str]:
    """Advisory findings for references nested deeper than one level."""
    findings: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        text = str(ref or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        if is_reference_too_deep(text):
            findings.append(
                f"文件引用 '{text}' 距 SKILL.md 超过一层"
                "（规范要求引用保持一层深，避免嵌套引用链）"
            )
    return findings


# --------------------------------------------------------------------------- #
# body-text composition (for volume estimation)                               #
# --------------------------------------------------------------------------- #
def _strings(value: Any, key: str) -> list[str]:
    """Pull a list of strings out of either ``{key: [...]}`` or a bare list."""
    if isinstance(value, Mapping):
        items = value.get(key) or []
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        items = []
    return [str(item) for item in items if str(item).strip()]


def contract_body_text(norm: Mapping[str, Any] | None) -> str:
    """Compose the SKILL.md *body* text a contract would render to.

    Only the body-bearing segments contribute (Procedure / Policies /
    Tool bindings / Examples); Manifest, Knowledge and Evaluation live in the
    frontmatter, the references index or an assets reference. Used purely for
    volume estimation — nothing is persisted here.
    """
    norm = norm or {}
    parts: list[str] = []
    parts += _strings(norm.get(SEGMENT_PROCEDURE), "steps")
    parts += _strings(norm.get(SEGMENT_POLICIES), "constraints")
    parts += _strings(norm.get(SEGMENT_TOOL_BINDINGS), "tools")
    parts += _strings(norm.get(SEGMENT_TOOL_BINDINGS), "scripts")
    parts += _strings(norm.get(SEGMENT_EXAMPLES), "examples")
    return "\n".join(parts)
