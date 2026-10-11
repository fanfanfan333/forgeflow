"""Concurrent learning must not race the idempotency/cooldown ledger."""

import asyncio

import pytest

from forgeflow.skills import evolution_loop


@pytest.mark.asyncio
async def test_same_skill_evolution_is_serialized_without_blocking_other_skills(monkeypatch):
    active = {}
    max_active = {}
    entered_other = asyncio.Event()

    async def evolve(tenant, skill, **kwargs):
        key = (tenant, skill)
        active[key] = active.get(key, 0) + 1
        max_active[key] = max(max_active.get(key, 0), active[key])
        if skill == "other":
            entered_other.set()
        else:
            # Prove that a different skill can run while this skill holds its lock.
            await asyncio.wait_for(entered_other.wait(), timeout=2)
        await asyncio.sleep(0)
        active[key] -= 1
        return skill

    monkeypatch.setattr(evolution_loop, "_maybe_evolve", evolve)
    outcomes = await asyncio.gather(
        evolution_loop.maybe_evolve("tenant", "same", actor="u"),
        evolution_loop.maybe_evolve("tenant", "same", actor="u"),
        evolution_loop.maybe_evolve("tenant", "other", actor="u"),
    )
    assert outcomes == ["same", "same", "other"]
    assert max_active[("tenant", "same")] == 1
    assert len(evolution_loop._EVOLUTION_LOCKS) == 0
