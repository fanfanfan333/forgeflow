"""Service-level objectives — three tiers + attainment (INC2 A2).

Targets are the values frozen in architecture §7.2:

    critical  99.5% availability / p95 1500 ms  → degrade: none
    important 99.0% availability / p95 2000 ms  → degrade: keyword_search
    edge      97.0% availability / p95 5000 ms  → degrade: disable_noncritical

They are **code constants** overridable through the ``SLO_*`` settings (the
config layer exposes ``slo_{critical,important,edge}_{availability,p95_ms}``),
so ops can retune a tier without a deploy.

Observations come from :mod:`forgeflow.observability.metrics_source` — the same
source the dashboard KPIs use. The profile only exposes an aggregate success
rate and a mean latency, so ``p95_actual_ms`` is approximated by the mean
latency and documented as such; a real percentile needs a histogram source.

Honesty: a tier with no observations reports ``attainment=None``,
``breaching=False`` and ``has_data=False`` — we never claim a breach (or a
perfect score) from an empty window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from forgeflow.config import get_settings
from forgeflow.cost.degrade import trigger_degrade

logger = logging.getLogger(__name__)

__all__ = [
    "SLO_TIERS",
    "CAPABILITIES",
    "TIER_DEGRADE_ACTIONS",
    "SloTier",
    "SloRegistry",
    "tiers_from_settings",
]


# Frozen defaults (§7.2). ``degrade`` keeps the architecture's action name.
SLO_TIERS: dict[str, dict[str, Any]] = {
    "critical": {"availability": 0.995, "p95_latency_ms": 1500, "degrade": "none"},
    "important": {"availability": 0.990, "p95_latency_ms": 2000, "degrade": "keyword_search"},
    "edge": {"availability": 0.970, "p95_latency_ms": 5000, "degrade": "disable_noncritical"},
}

# Capability → tier (§2.2), kept for callers that want to classify a feature.
CAPABILITIES: dict[str, str] = {
    "task_submit": "critical",
    "sse": "critical",
    "policy": "critical",
    "tenant": "critical",
    "audit": "critical",
    "memory_search": "important",
    "skill_install": "important",
    "eval": "important",
    "analytics": "edge",
    "advice": "edge",
    "evolution": "edge",
    "multimodal": "edge",
}

# Architecture tier-degrade name → concrete actions from cost/degrade.py.
TIER_DEGRADE_ACTIONS: dict[str, tuple[str, ...]] = {
    "none": (),
    "keyword_search": ("notify", "swap_model"),
    "disable_noncritical": ("notify", "pause_noncritical"),
}


def tiers_from_settings(settings: Any | None = None) -> dict[str, dict[str, Any]]:
    """Effective tiers: frozen defaults, overridden by the ``SLO_*`` settings."""
    settings = settings or get_settings()
    tiers: dict[str, dict[str, Any]] = {
        name: dict(values) for name, values in SLO_TIERS.items()
    }
    overrides = {
        "critical": (
            getattr(settings, "slo_critical_availability", None),
            getattr(settings, "slo_critical_p95_ms", None),
        ),
        "important": (
            getattr(settings, "slo_important_availability", None),
            getattr(settings, "slo_important_p95_ms", None),
        ),
        "edge": (
            getattr(settings, "slo_edge_availability", None),
            getattr(settings, "slo_edge_p95_ms", None),
        ),
    }
    for tier, (availability, p95_ms) in overrides.items():
        if tier not in tiers:
            continue
        if availability is not None:
            tiers[tier]["availability"] = float(availability)
        if p95_ms is not None:
            tiers[tier]["p95_latency_ms"] = int(p95_ms)
    return tiers


@dataclass(frozen=True)
class SloTier:
    """Target vs. observation for one tier."""

    tier: str
    availability_target: float
    availability_actual: float | None = None
    p95_target_ms: int = 0
    p95_actual_ms: float | None = None
    attainment: float | None = None
    breaching: bool = False
    has_data: bool = False
    degrade: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "availability_target": self.availability_target,
            "availability_actual": self.availability_actual,
            "p95_target_ms": self.p95_target_ms,
            "p95_actual_ms": self.p95_actual_ms,
            "attainment": self.attainment,
            "breaching": self.breaching,
            "has_data": self.has_data,
            "degrade": self.degrade,
        }


class SloRegistry:
    """Computes per-tier attainment and fires degrade actions on breach."""

    def __init__(
        self,
        tenant_id: str | None = None,
        *,
        source: Any | None = None,
        fire_degrade: bool = True,
    ) -> None:
        self._tenant_id = tenant_id
        self._source = source
        self._fire_degrade = fire_degrade

    @property
    def source(self) -> Any:
        if self._source is None:
            from forgeflow.observability.metrics_source import get_metrics_source

            self._source = get_metrics_source()
        return self._source

    # -- pure computation ----------------------------------------------------
    @staticmethod
    def compute_tier(
        tier: str,
        *,
        availability_target: float,
        p95_target_ms: int,
        availability_actual: float | None,
        p95_actual_ms: float | None,
        degrade: str = "none",
    ) -> SloTier:
        """Attainment = the worse of the availability and latency ratios.

        Both ratios are capped at 1.0 so a tier can never read above 100%.
        With no observations we return ``attainment=None`` /
        ``breaching=False`` / ``has_data=False`` rather than inventing a number.
        """
        has_data = availability_actual is not None or p95_actual_ms is not None
        if not has_data:
            return SloTier(
                tier=tier,
                availability_target=availability_target,
                p95_target_ms=p95_target_ms,
                degrade=degrade,
            )

        ratios: list[float] = []
        if availability_actual is not None:
            ratios.append(
                min(1.0, availability_actual / availability_target)
                if availability_target > 0
                else 1.0
            )
        if p95_actual_ms is not None:
            ratios.append(
                min(1.0, p95_target_ms / p95_actual_ms)
                if p95_actual_ms > 0
                else 1.0
            )
        attainment = min(ratios) if ratios else None

        breaching = False
        if availability_actual is not None and availability_actual < availability_target:
            breaching = True
        if p95_actual_ms is not None and p95_target_ms and p95_actual_ms > p95_target_ms:
            breaching = True

        return SloTier(
            tier=tier,
            availability_target=availability_target,
            availability_actual=availability_actual,
            p95_target_ms=p95_target_ms,
            p95_actual_ms=p95_actual_ms,
            attainment=attainment,
            breaching=breaching,
            has_data=True,
            degrade=degrade,
        )

    # -- observation ---------------------------------------------------------
    async def tiers(self) -> list[SloTier]:
        """Read the window from ``MetricsSource`` and grade every tier."""
        payload = await self.source.summary(self._tenant_id)
        has_success = bool(payload.get("has_success_rate", True))
        has_latency = bool(payload.get("has_latency", True))
        has_data = bool(payload.get("has_data", False))

        availability_actual = float(payload.get("success_rate") or 0.0) if (
            has_data and has_success
        ) else None
        p95_actual = float(payload.get("avg_latency_ms") or 0.0) if (
            has_data and has_latency
        ) else None

        results: list[SloTier] = []
        for tier, values in tiers_from_settings().items():
            slo = self.compute_tier(
                tier,
                availability_target=float(values["availability"]),
                p95_target_ms=int(values["p95_latency_ms"]),
                availability_actual=availability_actual,
                p95_actual_ms=p95_actual,
                degrade=str(values["degrade"]),
            )
            if slo.breaching and self._fire_degrade:
                self._fire(slo)
            results.append(slo)
        return results

    def _fire(self, slo: SloTier) -> None:
        actions = TIER_DEGRADE_ACTIONS.get(slo.degrade, ())
        if not actions:
            return
        trigger_degrade(
            slo.tier,
            actions,
            reason=f"SLO breach ({slo.degrade})",
            context={"attainment": slo.attainment, "tier": slo.tier},
        )

    async def summary(self) -> dict[str, Any]:
        """``GET /metrics/slo`` payload."""
        return {
            "tiers": [t.to_dict() for t in await self.tiers()],
            "source": "metrics_source",
        }
