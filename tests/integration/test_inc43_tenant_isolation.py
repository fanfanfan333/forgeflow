"""INC43 §3.4 — BE-5 tenant isolation nails for the Skill Hub.

Tenant isolation is a **security property** that must hold *below* RBAC, in the
repository layer (INC43 §3.4). The four BE-5 nails the design calls out are
pinned here:

  1. Discovery (``GET /skills`` / ``GET /skill-candidates`` / ``GET /experiences``)
     repository reads with ``tenant_id is None`` return the **empty set**
     (fail-closed) — never every tenant's rows.
  2. Selection (``skills/registry.select``) only ever ranks inside the already
     tenant-filtered candidate set — a foreign tenant's skill id is never chosen.
  3. ``admin`` cannot bypass: ``tenant_scope.require_tenant`` is role-agnostic and
     rejects an unresolved tenant with ``403`` at the service/router entry.
  4. Counterfactual injection: temporarily relaxing the repository filter must
     turn these nails red (see ``qa_tmp/inc43_t03_p2_inject.py`` — run separately;
     the product code is restored afterwards).

Each case runs on **both** backends (``memory`` / ``postgres``). The ``memory``
parameter declares its profile (``conftest.force_memory_backend``); the
``postgres`` parameter migrates the dev database to ``head`` and skips cleanly
when no server is reachable. Assertions are membership-based (``my marker ∈ my
view`` / ``my marker ∉ their view`` / ``∉ the unresolved-tenant view``), so
left-over rows can never make a case vacuously green — and a relaxed filter
makes ``my marker ∈ the unresolved-tenant view`` true and goes red.

Cleanup: the ``postgres`` parameter deletes **only the exact rows a case wrote**
(by id), never a tenant-wide sweep of the shared dev database. The ``memory``
parameter writes exclusively under ``t-alpha`` / ``t-beta`` (never the
``"default"`` sentinel), so it needs no store reset and cannot disturb the
baseline suites.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import uuid

import pytest

from forgeflow.config import get_settings
from forgeflow.experience.models import ExperienceRecord
from forgeflow.skills.models import SkillCandidateRecord, SkillRecord

# NOTE: no module-level ``pytest.mark.asyncio`` — the suite runs in
# ``asyncio_mode=auto`` (see pyproject.toml), so async cases are detected
# automatically and the two sync cases (the gate + lifecycle nails) are not
# spuriously marked.

_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Two distinct, **non-UUID** tenants. A non-UUID slug is the exact case the
#: retired UUID coercion used to collapse into one shared NULL bucket, so these
#: ids are the sharpest test of real (string) isolation.
TENANT_A = "t-alpha"
TENANT_B = "t-beta"


def _suffix() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- #
# profile activation (mirrors tests/integration/test_tenant_isolation_conformance)#
# --------------------------------------------------------------------------- #
def _postgres_reachable() -> bool:
    import psycopg

    dsn = get_settings().postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")
    try:
        with psycopg.connect(dsn, connect_timeout=4) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:  # noqa: BLE001 — any failure means "not usable here"
        return False


def _upgrade_head() -> None:
    child_env = dict(os.environ)
    child_env["POSTGRES_SYNC_URL"] = get_settings().postgres_sync_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_ROOT),
        env=child_env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{result.stderr[-2000:]}")


def _require_postgres() -> None:
    if not _postgres_reachable():
        pytest.skip("live Postgres (5433) not reachable — cross-backend case needs it")
    try:
        _upgrade_head()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"dev DB could not be migrated to head: {exc}")


@pytest.fixture
def active_backend(request, monkeypatch):
    """Activate the parametrised storage backend for one case (see conformance)."""
    backend = request.param
    if backend == "memory":
        request.getfixturevalue("force_memory_backend")
        yield "memory"
        return

    _require_postgres()
    from forgeflow.observability.metrics_source import reset_metrics_source
    from forgeflow.repositories.factory import reset_repositories

    monkeypatch.setattr(get_settings(), "storage_backend", "postgres")
    reset_metrics_source()
    reset_repositories()
    try:
        yield "postgres"
    finally:
        reset_metrics_source()
        reset_repositories()


# --------------------------------------------------------------------------- #
# marker writers / cleanup                                                     #
# --------------------------------------------------------------------------- #
async def _write_skill(tenant: str) -> SkillRecord:
    from forgeflow.repositories import get_skill_repository

    skill = SkillRecord(
        tenant_id=tenant,
        name=f"iso-{_suffix()}",
        domain="general",
        description=f"tenant marker {_suffix()}",
        status="published",
        current_version="1.0.0",
    )
    await get_skill_repository().create_skill(skill)
    return skill


async def _write_candidate(tenant: str) -> SkillCandidateRecord:
    from forgeflow.repositories import get_skill_candidate_repository

    candidate = SkillCandidateRecord(
        tenant_id=tenant, name=f"iso-{_suffix()}", domain="general", status="draft"
    )
    await get_skill_candidate_repository().save_candidate(candidate)
    return candidate


async def _write_experience(tenant: str) -> ExperienceRecord:
    from forgeflow.repositories import get_experience_repository

    record = ExperienceRecord(tenant_id=tenant, summary=f"iso-{_suffix()}", outcome="success")
    await get_experience_repository().save(record)
    return record


def _cleanup_postgres(rows: list[tuple[str, str]]) -> None:
    """Best-effort removal of **only the rows this case wrote** (by id)."""
    if get_settings().storage_backend.lower() != "postgres" or not rows:
        return
    import psycopg

    dsn = get_settings().postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")
    by_table: dict[str, list[str]] = {}
    for table, row_id in rows:
        by_table.setdefault(table, []).append(row_id)
    try:
        with psycopg.connect(dsn, connect_timeout=4) as conn:
            with conn.cursor() as cur:
                for table, ids in by_table.items():
                    cur.execute(f"DELETE FROM {table} WHERE id::text = ANY(%s)", (ids,))
            conn.commit()
    except Exception:  # noqa: BLE001 — cleanup is strictly best-effort
        return


def _cleanup(active_backend: str, rows: list[tuple[str, str]]) -> None:
    if active_backend == "postgres":
        _cleanup_postgres(rows)


# --------------------------------------------------------------------------- #
# BE-5 nail 3 — the gate itself (role-agnostic, fail-closed)                    #
# --------------------------------------------------------------------------- #
def test_require_tenant_is_fail_closed() -> None:
    from forgeflow.skills.errors import GovernanceError
    from forgeflow.skills.tenant_scope import require_tenant, scope_filter

    # An unresolved tenant is a hard 403 — not an empty-string default.
    with pytest.raises(GovernanceError) as excinfo_none:
        require_tenant(None)
    assert excinfo_none.value.status_code == 403
    with pytest.raises(GovernanceError) as excinfo_empty:
        require_tenant("")
    assert excinfo_empty.value.status_code == 403

    # A resolved tenant is returned unchanged (so callers can chain).
    assert require_tenant(TENANT_A) == TENANT_A

    # A record with no tenant matches NO tenant (never "global"/all-tenants).
    assert scope_filter(None, TENANT_A) is False
    assert scope_filter(TENANT_A, TENANT_A) is True
    assert scope_filter(TENANT_B, TENANT_A) is False


# --------------------------------------------------------------------------- #
# BE-5 nail 1 — unresolved-tenant reads are empty on every repo (the "leak" gate)#
# One case per repository so each repo's fail-closed guard is pinned *and*      #
# independently shown load-bearing by the counterfactual injection.            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("active_backend", ["memory", "postgres"], indirect=True)
async def test_skill_repo_reads_are_fail_closed_for_unresolved_tenant(active_backend) -> None:
    from forgeflow.repositories import get_skill_repository

    cleanup: list[tuple[str, str]] = []
    try:
        skill = await _write_skill(TENANT_A)
        cleanup.append(("skills", skill.id))
        repo = get_skill_repository()

        assert await repo.get_skill(None, skill.id) is None
        rows, total = await repo.list_skills(None, limit=500)
        assert rows == [] and total == 0
        assert await repo.get_skill_by_name(None, skill.name) is None
        assert await repo.list_versions(None, skill.id) == []
    finally:
        _cleanup(active_backend, cleanup)


@pytest.mark.parametrize("active_backend", ["memory", "postgres"], indirect=True)
async def test_candidate_repo_reads_are_fail_closed_for_unresolved_tenant(active_backend) -> None:
    from forgeflow.repositories import get_skill_candidate_repository

    cleanup: list[tuple[str, str]] = []
    try:
        candidate = await _write_candidate(TENANT_A)
        cleanup.append(("skill_candidates", candidate.id))
        repo = get_skill_candidate_repository()

        assert await repo.get_candidate(None, candidate.id) is None
        assert await repo.list_candidates(None, limit=500) == []
        assert await repo.list_candidate_experiences(None, candidate.id) == []
        assert await repo.get_evaluation_for(None, candidate.id) is None
    finally:
        _cleanup(active_backend, cleanup)


@pytest.mark.parametrize("active_backend", ["memory", "postgres"], indirect=True)
async def test_experience_repo_reads_are_fail_closed_for_unresolved_tenant(active_backend) -> None:
    from forgeflow.repositories import get_experience_repository

    cleanup: list[tuple[str, str]] = []
    try:
        experience = await _write_experience(TENANT_A)
        cleanup.append(("experiences", experience.id))
        repo = get_experience_repository()

        assert await repo.get(None, experience.id) is None
        assert await repo.list(None, limit=500) == []
        assert await repo.find_similar(None, None, tags=["iso"]) == []
        assert await repo.count(None) == 0
    finally:
        _cleanup(active_backend, cleanup)


# --------------------------------------------------------------------------- #
# BE-5 nail 1b — pairwise isolation (own row visible, other tenant's never)     #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("active_backend", ["memory", "postgres"], indirect=True)
async def test_skill_repo_isolates_tenant_markers(active_backend) -> None:
    from forgeflow.repositories import get_skill_repository

    cleanup: list[tuple[str, str]] = []
    try:
        mine = await _write_skill(TENANT_A)
        theirs = await _write_skill(TENANT_B)
        cleanup += [("skills", mine.id), ("skills", theirs.id)]
        repo = get_skill_repository()

        ids_a = {r.id for r in (await repo.list_skills(TENANT_A, limit=500))[0]}
        assert mine.id in ids_a, "owner cannot read back its own skill"
        assert theirs.id not in ids_a, "cross-tenant leak: A saw B's skill"

        ids_b = {r.id for r in (await repo.list_skills(TENANT_B, limit=500))[0]}
        assert theirs.id in ids_b
        assert mine.id not in ids_b, "cross-tenant leak: B saw A's skill"
    finally:
        _cleanup(active_backend, cleanup)


@pytest.mark.parametrize("active_backend", ["memory", "postgres"], indirect=True)
async def test_candidate_and_experience_repos_isolate_tenant_markers(active_backend) -> None:
    from forgeflow.repositories import (
        get_experience_repository,
        get_skill_candidate_repository,
    )

    cleanup: list[tuple[str, str]] = []
    try:
        cand_a = await _write_candidate(TENANT_A)
        cand_b = await _write_candidate(TENANT_B)
        exp_a = await _write_experience(TENANT_A)
        exp_b = await _write_experience(TENANT_B)
        cleanup += [
            ("skill_candidates", cand_a.id),
            ("skill_candidates", cand_b.id),
            ("experiences", exp_a.id),
            ("experiences", exp_b.id),
        ]

        cand_repo = get_skill_candidate_repository()
        cand_ids_a = {c.id for c in await cand_repo.list_candidates(TENANT_A, limit=500)}
        assert cand_a.id in cand_ids_a and cand_b.id not in cand_ids_a
        cand_ids_b = {c.id for c in await cand_repo.list_candidates(TENANT_B, limit=500)}
        assert cand_b.id in cand_ids_b and cand_a.id not in cand_ids_b

        exp_repo = get_experience_repository()
        exp_ids_a = {e.id for e in await exp_repo.list(TENANT_A, limit=500)}
        assert exp_a.id in exp_ids_a and exp_b.id not in exp_ids_a
        exp_ids_b = {e.id for e in await exp_repo.list(TENANT_B, limit=500)}
        assert exp_b.id in exp_ids_b and exp_a.id not in exp_ids_b
    finally:
        _cleanup(active_backend, cleanup)


# --------------------------------------------------------------------------- #
# BE-5 nail 2 — Selection ranks only inside the filtered candidate set          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("active_backend", ["memory", "postgres"], indirect=True)
async def test_registry_select_is_confined_to_filtered_candidates(active_backend) -> None:
    from forgeflow.repositories import get_skill_repository
    from forgeflow.skills.registry import SkillRegistry

    cleanup: list[tuple[str, str]] = []
    try:
        mine = await _write_skill(TENANT_A)
        cleanup.append(("skills", mine.id))
        registry = SkillRegistry()

        picked_foreign = await registry.select(TENANT_B, mine.name, k=10)
        assert mine.id not in {s.id for s in picked_foreign}, (
            "selection leaked a foreign tenant's skill into the candidate set"
        )

        picked_owner = await registry.select(TENANT_A, mine.name, k=10)
        assert mine.id in {s.id for s in picked_owner}
    finally:
        _cleanup(active_backend, cleanup)


# --------------------------------------------------------------------------- #
# BE-4 — illegal lifecycle transitions are fail-closed (403)                    #
# --------------------------------------------------------------------------- #
def test_illegal_lifecycle_transition_is_fail_closed() -> None:
    from forgeflow.skills.engineering import assert_transition, can_transition
    from forgeflow.skills.errors import GovernanceError

    # Skipping TESTING/REVIEW to PUBLISHED is illegal and must be a hard 403.
    assert can_transition("DRAFT", "PUBLISHED") is False
    with pytest.raises(GovernanceError) as excinfo:
        assert_transition("DRAFT", "PUBLISHED")
    assert excinfo.value.status_code == 403

    # A legal, human-gated retire remains legal.
    assert can_transition("PUBLISHED", "DEPRECATED") is True


# --------------------------------------------------------------------------- #
# Memory-only sync accessor the candidate compiler's clustering step consumes   #
# --------------------------------------------------------------------------- #
async def test_memory_all_records_is_fail_closed_for_unresolved_tenant(
    force_memory_backend,
) -> None:
    from forgeflow.repositories.memory.experience_repo import MemoryExperienceRepository

    repo = MemoryExperienceRepository()
    record = ExperienceRecord(tenant_id=TENANT_A, summary=f"iso-{_suffix()}", outcome="success")
    await repo.save(record)

    # Sanity: the row really is there for its owner (the case is not vacuous).
    assert record.id in {r.id for r in repo.all_records(TENANT_A)}
    # Fail-closed: an unresolved tenant sees nothing to cluster over.
    assert repo.all_records(None) == []
