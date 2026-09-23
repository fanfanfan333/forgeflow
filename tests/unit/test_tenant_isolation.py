"""Tenant isolation — cross-tenant read=empty/None, write=PermissionError (P0-10)."""

from __future__ import annotations

import uuid

import pytest

from forgeflow.experience.models import ExperienceRecord
from forgeflow.governance.tenancy import TenantIsolation, assert_tenant
from forgeflow.repositories.memory.experience_repo import MemoryExperienceRepository

pytestmark = pytest.mark.asyncio


def _tenant() -> str:
    return f"t-iso-{uuid.uuid4().hex[:8]}"


def test_assert_access_blocks_cross_tenant():
    iso = TenantIsolation()
    iso.assert_access("tenant-a", "tenant-a")  # same tenant — ok
    with pytest.raises(PermissionError):
        iso.assert_access("tenant-a", "tenant-b")
    assert iso.can_access("a", "a") is True
    assert iso.can_access("a", "b") is False


def test_assert_tenant_function_helper():
    with pytest.raises(PermissionError):
        assert_tenant("a", "b")


def test_filter_rows_keeps_owner_only():
    iso = TenantIsolation()
    rows = [{"tenant_id": "a"}, {"tenant_id": "b"}]
    kept = iso.filter_rows(rows, "a")
    assert kept == [{"tenant_id": "a"}]


async def test_repository_reads_are_tenant_scoped():
    repo = MemoryExperienceRepository()
    tenant_a = _tenant()
    tenant_b = _tenant()

    rec = ExperienceRecord(tenant_id=tenant_a, run_id="r", summary="s", outcome="success")
    await repo.save(rec)

    # same tenant can read it, another tenant cannot (→ None), count is per-tenant
    assert await repo.get(tenant_a, rec.id) is not None
    assert await repo.get(tenant_b, rec.id) is None
    assert await repo.count(tenant_a) == 1
    assert await repo.count(tenant_b) == 0


async def test_repository_write_guard_raises_on_mismatch():
    repo = MemoryExperienceRepository()
    with pytest.raises(PermissionError):
        repo.assert_same_tenant("tenant-a", "tenant-b")
