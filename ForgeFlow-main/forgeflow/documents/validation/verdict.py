"""INC46 T23 — the verdict data model shared by every validation layer.

The five-layer stack (design §「五层验证栈」) reports **one verdict per layer**:

    {"layer": "L1", "status": "pass" | "fail" | None, "evidence_ref": "…"}

``status`` is the load-bearing honesty rule (red line 15 / red line 4):

  * ``"pass"``  — the layer ran and the document satisfied it;
  * ``"fail"``  — the layer ran and the document violated it;
  * ``None``    — the layer **could not measure** (engine / judge unavailable).
    ``None`` is never dressed up as ``"pass"``.

``L5`` additionally uses ``"needs_review"`` for a low semantic score (advisory
only, never a standalone release — see :mod:`forgeflow.documents.validation.stack`).

``evidence_ref`` must point at a **real, reproducible fact** (a document.xml
paragraph index, a table cell coordinate, a mutation-probe name) — never an empty
string or a placeholder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PASS",
    "FAIL",
    "NEEDS_REVIEW",
    "LAYER_STRUCTURAL",
    "LAYER_INVARIANTS",
    "LAYER_FORMAT",
    "LAYER_RENDER",
    "LAYER_SEMANTIC",
    "LAYER_ORDER",
    "LayerVerdict",
    "ValidationConfig",
    "ValidationVerdict",
]

#: Layer status vocabulary.
PASS = "pass"
FAIL = "fail"
NEEDS_REVIEW = "needs_review"

#: Layer identifiers (§「五层验证栈」).
LAYER_STRUCTURAL = "L1"
LAYER_INVARIANTS = "L2"
LAYER_FORMAT = "L3"
LAYER_RENDER = "L4"
LAYER_SEMANTIC = "L5"

#: Canonical evaluation order (L1→L5).
LAYER_ORDER: tuple[str, ...] = (
    LAYER_STRUCTURAL,
    LAYER_INVARIANTS,
    LAYER_FORMAT,
    LAYER_RENDER,
    LAYER_SEMANTIC,
)


@dataclass
class LayerVerdict:
    """One validation layer's tri-state verdict (``status`` may be ``None``)."""

    layer: str
    status: str | None
    evidence_ref: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def measured(self) -> bool:
        """True iff the layer actually measured something (status is not None)."""
        return self.status is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "status": self.status,
            "evidence_ref": self.evidence_ref,
            "detail": dict(self.detail),
        }


@dataclass
class ValidationConfig:
    """Stack configuration (all defaults are the honest, conservative choice).

    ``l4_fail_is_fatal`` — the task book leaves the render-layer failure policy
    configurable ("L4 fail ⇒ fail 或 warn（按配置）"). Default ``False`` ⇒ a
    render mismatch is reported as ``warn`` (visible, non-fatal).

    ``l5_review_threshold`` — a semantic score **below** this floor raises
    ``needs_review`` (advisory only; never a standalone release).

    ``disabled_layers`` — the counterfactual injection point: a disabled layer is
    reported as ``None`` (unmeasured), so the matching negative probe goes red.
    """

    l4_fail_is_fatal: bool = False
    l5_review_threshold: float = 0.6
    disabled_layers: frozenset[str] = frozenset()

    def is_disabled(self, layer: str) -> bool:
        return layer in self.disabled_layers


@dataclass
class ValidationVerdict:
    """The whole-stack verdict: per-layer verdicts + the aggregate ``overall``.

    ``overall`` ∈ ``{"pass", "fail", "warn", "needs_review"}``. ``unmeasured``
    lists the layers whose status is ``None`` so an aggregate ``pass`` can never
    hide an unmeasured layer (red line 15).
    """

    layers: dict[str, LayerVerdict]
    overall: str
    unmeasured: list[str] = field(default_factory=list)
    notes: str = ""

    def layer(self, name: str) -> LayerVerdict:
        return self.layers[name]

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall,
            "unmeasured": list(self.unmeasured),
            "notes": self.notes,
            "layers": {name: verdict.to_dict() for name, verdict in self.layers.items()},
        }
