"""INC2-07 (A2) — three-tier SLO, attainment, and breach-triggered degrade."""

from __future__ import annotations

import pytest

from forgeflow.api.routers.metrics import get_slo_summary
from forgeflow.cost.degrade import clear_degrade_callbacks, register_degrade_callback
from forgeflow.observability.slo import (
    SLO_TIERS,
    SloRegistry,
    SloTier,
    TIER_DEGRADE_ACTIONS,
    tiers_from_settings,
)

_TENANT = "tenant-slo"


class _FakeSource:
    """A MetricsSource stand-in with a fixed observation window."""

    def __init__(
        self,
        *,
        success_rate: float | None = None,
        avg_latency_ms: float | None = None,
        has_data: bool = True,
    ) -> None:
        self._success_rate = success_rate
        self._avg_latency_ms = avg_latency_ms
        self._has_data = has_data

    async def summary(self, tenant_id):
        return {
            "total_runs": 10 if self._has_data else 0,
            "terminal_runs": 10 if self._has_data else 0,
            "success_rate": self._success_rate or 0.0,
            "avg_latency_ms": self._avg_latency_ms or 0.0,
            "has_data": self._has_data,
            "has_cost": False,
            "has_success_rate": self._success_rate is not None,
            "has_latency": self._avg_latency_ms is not None,
            "source": "fake",
        }

    async def recent_runs(self, tenant_id, limit=20, *, workspace_id=None):
        return []


class TestFrozenTiers:
    def test_defaults_are_the_frozen_values(self):
        tiers = tiers_from_settings()

        assert tiers["critical"]["availability"] == pytest.approx(0.995)
        assert tiers["critical"]["p95_latency_ms"] == 1500
        assert tiers["important"]["availability"] == pytest.approx(0.990)
        assert tiers["important"]["p95_latency_ms"] == 2000
        assert tiers["edge"]["availability"] == pytest.approx(0.970)
        assert tiers["edge"]["p95_latency_ms"] == 5000

    def test_settings_override_the_defaults(self, monkeypatch):
        from forgeflow.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SLO_EDGE_AVAILABILITY", "0.900")
        try:
            assert tiers_from_settings()["edge"]["availability"] == pytest.approx(0.900)
        finally:
            get_settings.cache_clear()

    def test_every_tier_has_a_degrade_mapping(self):
        for values in SLO_TIERS.values():
            assert values["degrade"] in TIER_DEGRADE_ACTIONS


class TestAttainment:
    def test_meeting_target_is_full_attainment(self):
        tier = SloRegistry.compute_tier(
            "critical",
            availability_target=0.995,
            p95_target_ms=1500,
            availability_actual=1.0,
            p95_actual_ms=500.0,
        )

        assert tier.attainment == pytest.approx(1.0)
        assert tier.breaching is False
        assert tier.has_data is True

    def test_below_availability_target_is_breaching(self):
        tier = SloRegistry.compute_tier(
            "critical",
            availability_target=0.995,
            p95_target_ms=1500,
            availability_actual=0.90,
            p95_actual_ms=500.0,
        )

        assert tier.breaching is True
        assert tier.attainment == pytest.approx(0.90 / 0.995)

    def test_above_p95_target_is_breaching(self):
        tier = SloRegistry.compute_tier(
            "edge",
            availability_target=0.970,
            p95_target_ms=5000,
            availability_actual=1.0,
            p95_actual_ms=10_000.0,
        )

        assert tier.breaching is True
        assert tier.attainment == pytest.approx(5000 / 10_000)

    def test_attainment_is_the_worse_of_the_two_ratios(self):
        tier = SloRegistry.compute_tier(
            "important",
            availability_target=0.990,
            p95_target_ms=2000,
            availability_actual=0.95,
            p95_actual_ms=4000.0,
        )

        assert tier.attainment == pytest.approx(min(0.95 / 0.990, 2000 / 4000))

    def test_attainment_is_capped_at_one(self):
        tier = SloRegistry.compute_tier(
            "critical",
            availability_target=0.995,
            p95_target_ms=1500,
            availability_actual=1.0,
            p95_actual_ms=1.0,
        )

        assert tier.attainment == pytest.approx(1.0)

    def test_no_observations_is_not_a_breach(self):
        tier = SloRegistry.compute_tier(
            "critical",
            availability_target=0.995,
            p95_target_ms=1500,
            availability_actual=None,
            p95_actual_ms=None,
        )

        assert tier.has_data is False
        assert tier.attainment is None
        assert tier.breaching is False


class TestBreachTriggersDegrade:
    @pytest.mark.asyncio
    async def test_breaching_tier_fires_the_mapped_actions(self):
        fired: list[tuple[str, tuple[str, ...]]] = []
        clear_degrade_callbacks()
        register_degrade_callback(lambda t, a, ctx: fired.append((t, a)))

        registry = SloRegistry(
            _TENANT,
            source=_FakeSource(success_rate=0.50, avg_latency_ms=20_000.0),
        )
        try:
            tiers = await registry.tiers()
        finally:
            clear_degrade_callbacks()

        assert all(t.breaching for t in tiers)
        # critical degrades to "none" ⇒ no actions; the others do fire.
        assert fired, "a breaching tier must trigger a degrade callback"
        fired_tiers = {t for t, _ in fired}
        assert "important" in fired_tiers
        assert "edge" in fired_tiers
        assert "critical" not in fired_tiers

    @pytest.mark.asyncio
    async def test_healthy_window_fires_nothing(self):
        fired: list[tuple[str, tuple[str, ...]]] = []
        clear_degrade_callbacks()
        register_degrade_callback(lambda t, a, ctx: fired.append((t, a)))

        registry = SloRegistry(
            _TENANT,
            source=_FakeSource(success_rate=1.0, avg_latency_ms=10.0),
        )
        try:
            tiers = await registry.tiers()
        finally:
            clear_degrade_callbacks()

        assert not any(t.breaching for t in tiers)
        assert fired == []

    @pytest.mark.asyncio
    async def test_empty_window_fires_nothing(self):
        fired: list[tuple[str, tuple[str, ...]]] = []
        clear_degrade_callbacks()
        register_degrade_callback(lambda t, a, ctx: fired.append((t, a)))

        registry = SloRegistry(_TENANT, source=_FakeSource(has_data=False))
        try:
            tiers = await registry.tiers()
        finally:
            clear_degrade_callbacks()

        assert all(not t.has_data for t in tiers)
        assert fired == []


class TestEndpoint:
    @pytest.mark.asyncio
    async def test_slo_endpoint_returns_three_tiers(self):
        payload = await get_slo_summary(tenant=_TENANT)

        assert len(payload.tiers) == 3
        assert {t.tier for t in payload.tiers} == {"critical", "important", "edge"}
        assert all(isinstance(t, SloTier) or hasattr(t, "tier") for t in payload.tiers)
