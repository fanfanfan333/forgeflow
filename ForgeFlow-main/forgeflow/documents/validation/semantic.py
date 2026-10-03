"""INC46 T23 — **L5** semantic review (LLM-as-judge，仅建议、绝不单独放行).

What L5 checks
--------------
L1–L4 are mechanical. L5 adds the one judgement only a language model can make:
*does the edited text actually read like the requested result?* It scores four
explicit dimensions:

  * **风格达标** — the requested style goal (e.g. 「更正式」) is met;
  * **事实未变** — the facts survive (no re-writing of substance);
  * **无新增数字/名称** — no invented number or name appears;
  * **语言通顺** — the result is fluent.

The layer is **advisory only** (red line 15): a score below the configured floor
raises ``needs_review``, which the stack turns into an aggregate ``needs_review``
— it is **never** a standalone release, and it can never upgrade a
mechanical ``fail`` into a ``pass``.

Unmeasured ≠ pass (red line 4 / 15)
-----------------------------------
If no judge is wired (the default), L5 returns ``status=None`` ("not measured") —
never a fabricated ``pass``. The judge is **injectable** (``judge=…`` or
:func:`set_default_judge`), so production can wire a real LLM judge while tests
inject a deterministic one and still run the *real* threshold/advisory logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from forgeflow.documents.validation.invariants import paragraph_texts
from forgeflow.documents.validation.verdict import (
    LAYER_SEMANTIC,
    NEEDS_REVIEW,
    PASS,
    LayerVerdict,
    ValidationConfig,
)

__all__ = [
    "DIMENSIONS",
    "SemanticJudge",
    "SemanticRequest",
    "SemanticScore",
    "semantic_verdict",
    "set_default_judge",
]

#: The four scored dimensions (fixed names — a judge returns one score per name).
DIMENSIONS: tuple[str, ...] = (
    "风格达标",
    "事实未变",
    "无新增数字/名称",
    "语言通顺",
)


@dataclass
class SemanticRequest:
    """Everything a judge may look at (text-only; the judge sees no raw XML)."""

    before_text: str
    after_text: str
    instruction: str | None = None
    style_goal: str | None = None


@dataclass
class SemanticScore:
    """A judge's verdict: an overall ``score`` + one score per :data:`DIMENSIONS`."""

    score: float
    dimensions: dict[str, float] = field(default_factory=dict)
    rationale: str = ""
    judge: str = ""

    def effective(self) -> float:
        """The conservative score: the minimum of the overall and every dimension."""
        values = [float(self.score)]
        values.extend(float(v) for v in self.dimensions.values())
        return min(values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "dimensions": dict(self.dimensions),
            "rationale": self.rationale,
            "judge": self.judge,
        }


@runtime_checkable
class SemanticJudge(Protocol):
    """A semantic judge: ``request -> score`` (synchronous, side-effect-free)."""

    def __call__(self, request: SemanticRequest) -> SemanticScore: ...


# --------------------------------------------------------------------------- #
# judge registry (default = none wired ⇒ L5 honestly unmeasured)               #
# --------------------------------------------------------------------------- #
_DEFAULT_JUDGE: SemanticJudge | None = None


def set_default_judge(judge: SemanticJudge | None) -> None:
    """Register (or clear) the process-wide default judge.

    The default is ``None`` — no judge is wired — which makes L5 honestly report
    ``None`` (not measured). Production wires a real LLM judge through this seam;
    tests inject one directly via ``semantic_verdict(..., judge=…)``.
    """
    global _DEFAULT_JUDGE
    _DEFAULT_JUDGE = judge


def _resolve_judge(judge: SemanticJudge | None) -> SemanticJudge | None:
    return judge if judge is not None else _DEFAULT_JUDGE


def _coerce_score(raw: Any) -> SemanticScore:
    """Accept a :class:`SemanticScore` or a mapping with the same fields."""
    if isinstance(raw, SemanticScore):
        return raw
    if isinstance(raw, dict):
        return SemanticScore(
            score=float(raw.get("score", 0.0)),
            dimensions={k: float(v) for k, v in dict(raw.get("dimensions") or {}).items()},
            rationale=str(raw.get("rationale", "")),
            judge=str(raw.get("judge", "")),
        )
    raise TypeError(f"judge 必须返回 SemanticScore 或 mapping，实得 {type(raw)!r}")


def _range_text(texts: list[str], lo: int, hi: int) -> str:
    return "\n".join(texts[lo:hi])


# --------------------------------------------------------------------------- #
# verdict                                                                      #
# --------------------------------------------------------------------------- #
def semantic_verdict(
    before: bytes,
    after: bytes,
    *,
    start: int = 0,
    end: int | None = None,
    instruction: str | None = None,
    style_goal: str | None = None,
    judge: SemanticJudge | None = None,
    config: ValidationConfig | None = None,
    judge_name: str | None = None,
) -> LayerVerdict:
    """L5 — semantic verdict between ``before`` and ``after`` over ``[start, end)``.

    Args:
        before: the original DOCX bytes.
        after: the edited DOCX bytes.
        start: the located range's first paragraph index (inclusive).
        end: the located range's exclusive end paragraph index (``None`` ⇒ EOF).
        instruction: the raw user instruction (context for the judge).
        style_goal: the parsed style goal (context for the judge).
        judge: an injectable judge (defaults to the registered one, else ``None``).
        config: the stack config (supplies ``l5_review_threshold``).
        judge_name: an override label recorded in the evidence.

    Returns:
        A :class:`LayerVerdict`; ``needs_review`` when the effective score is below
        the config floor, ``pass`` otherwise, and ``None`` (not measured) when no
        judge is available — never a fabricated ``pass``.
    """
    settings = config or ValidationConfig()
    resolved = _resolve_judge(judge)
    if resolved is None:
        return LayerVerdict(
            LAYER_SEMANTIC,
            None,
            "semantic:not measured (no judge wired)",
            {"judge": None},
        )

    before_texts = paragraph_texts(before)
    after_texts = paragraph_texts(after)
    length = len(before_texts)
    lo = max(0, min(int(start), length))
    hi = length if end is None else max(lo, min(int(end), length))

    request = SemanticRequest(
        before_text=_range_text(before_texts, lo, hi),
        after_text=_range_text(after_texts, lo, min(hi, len(after_texts))),
        instruction=instruction,
        style_goal=style_goal,
    )
    score = _coerce_score(resolved(request))
    effective = score.effective()
    threshold = float(settings.l5_review_threshold)
    status = NEEDS_REVIEW if effective < threshold else PASS
    label = judge_name or score.judge or "judge"
    dims = ";".join(f"{name}={score.dimensions.get(name, 'n/a')}" for name in DIMENSIONS)
    evidence = f"semantic:{label};effective={effective:.3f};threshold={threshold};{dims}"
    detail: dict[str, Any] = {
        "judge": label,
        "score": score.to_dict(),
        "effective": effective,
        "threshold": threshold,
        "advisory_only": True,
    }
    return LayerVerdict(LAYER_SEMANTIC, status, evidence, detail)
