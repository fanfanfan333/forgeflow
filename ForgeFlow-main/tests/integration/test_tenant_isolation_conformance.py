"""INC12-A2 — cross-backend tenant-isolation conformance (memory ⇄ PostgreSQL).

A tenant id is an **opaque string** (``RequestContext.tenant_id: str``, the
``"default"`` sentinel, slugs like ``"t-alpha"``). Before INC12-A2 the hub
tables declared ``tenant_id`` / ``team_id`` as ``UUID`` and each repository
coerced a non-UUID tenant to ``NULL`` — a silent **cross-tenant leak**: on
PostgreSQL the three tenant ids used below (``"default"``, ``"t-alpha"``,
``"t-beta"``) all collapsed onto one shared ``NULL`` bucket, so one tenant could
read another's rows. The memory backend (which stores the raw string) never did
this, so the two backends disagreed about isolation and no test caught it.

This file pins the corrected contract on **both** backends:

  * migration 013 widens every ``tenant_id`` / ``team_id`` column to ``TEXT``;
  * every hub repository isolates the three non-UUID tenants **pairwise** — a
    tenant reads back its own row and never another's.

Each case runs twice (parametrised ``memory`` / ``postgres``). The ``memory``
parameter **declares** its profile (via ``conftest.force_memory_backend``) rather
than inheriting it; the ``postgres`` parameter migrates the live dev database to
``head`` and skips cleanly when no server is reachable. Assertions are
membership-based (``my marker ∈ my view`` and ``their marker ∉ my view``), so
left-over rows from other suites can never make this vacuously green — and the
retired coercion would make ``their marker ∈ my view`` true and go red.

Non-vacuity: re-introducing the ``NULL`` coercion in any repository makes the
corresponding case fail on the ``postgres`` parameter (the memory parameter is
unaffected, which is itself the point the two backends had diverged).
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable

import pytest

from forgeflow.config import get_settings
from forgeflow.cost.models import CostBudgetRecord
from forgeflow.experience.models import ExperienceRecord
from forgeflow.governance.models import PolicyRecord
from forgeflow.repositories.eval_sample_repo import EvalSample
from forgeflow.skills.models import SkillCandidateRecord, SkillRecord

pytestmark = pytest.mark.asyncio

_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The exact non-UUID tenant ids the retired UUID coercion collapsed into one
#: shared ``NULL`` bucket. Distinct on every backend once migration 013 lands.
NON_UUID_TENANTS: tuple[str, ...] = ("default", "t-alpha", "t-beta")

# A unique suffix lets the same tenant id be written more than once
# (across runs / suites) without a marker being clobbered.
def _suffix() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- #
# profile activation                                                           #
# --------------------------------------------------------------------------- #
def _postgres_reachable() -> bool:
    """True when the configured dev Postgres answers a trivial query."""
    import psycopg

    dsn = get_settings().postgres_sync_url.replace(
        "postgresql+psycopg://", "postgresql://"
    )
    try:
        with psycopg.connect(dsn, connect_timeout=4) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:  # noqa: BLE001 — any failure means "not usable here"
        return False


def _upgrade_head() -> None:
    """Apply migrations to ``head`` in a subprocess (mirrors the PG test suites)."""
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
    """Skip cleanly unless a live, migratable dev Postgres is available."""
    if not _postgres_reachable():
        pytest.skip("live Postgres (5433) not reachable — cross-backend case needs it")
    try:
        _upgrade_head()
    except Exception as exc:  # noqa: BLE001 — an unusable DB means "skip here"
        pytest.skip(f"dev DB could not be migrated to head: {exc}")


@pytest.fixture
def active_backend(request, monkeypatch):
    """Activate the parametrised storage backend for one case.

    ``memory`` **declares** its profile through ``force_memory_backend`` (never
    inherits it). ``postgres`` requires a reachable, migrated database and pins
    ``storage_backend`` to ``postgres`` — so a single ``pytest`` invocation
    exercises both backends.
    """
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
# per-repository probes: write one row under a tenant, then read that tenant's  #
# visible id set                                                                 #
# --------------------------------------------------------------------------- #
Writer = Callable[[str], Awaitable[str]]
Reader = Callable[[str], Awaitable[set[str]]]


async def _write_experience(tenant: str) -> str:
    from forgeflow.repositories import get_experience_repository

    rec = ExperienceRecord(
        tenant_id=tenant,
        summary=f"iso-{_suffix()}",
        outcome="success",
    )
    await get_experience_repository().save(rec)
    return rec.id


async def _read_experience(tenant: str) -> set[str]:
    from forgeflow.repositories import get_experience_repository

    rows = await get_experience_repository().list(tenant, limit=500)
    return {r.id for r in rows}


async def _write_skill(tenant: str) -> str:
    from forgeflow.repositories import get_skill_repository

    skill = SkillRecord(
        tenant_id=tenant,
        name=f"iso-{_suffix()}",
        domain="general",
        status="published",
        current_version="1.0.0",
    )
    await get_skill_repository().create_skill(skill)
    return skill.id


async def _read_skill(tenant: str) -> set[str]:
    from forgeflow.repositories import get_skill_repository

    rows, _total = await get_skill_repository().list_skills(tenant, limit=500)
    return {r.id for r in rows}


async def _write_candidate(tenant: str) -> str:
    from forgeflow.repositories import get_skill_candidate_repository

    candidate = SkillCandidateRecord(
        tenant_id=tenant,
        name=f"iso-{_suffix()}",
        domain="general",
        status="draft",
    )
    await get_skill_candidate_repository().save_candidate(candidate)
    return candidate.id


async def _read_candidate(tenant: str) -> set[str]:
    from forgeflow.repositories import get_skill_candidate_repository

    rows = await get_skill_candidate_repository().list_candidates(tenant, limit=500)
    return {r.id for r in rows}


async def _write_policy(tenant: str) -> str:
    from forgeflow.repositories import get_policy_repository

    policy = PolicyRecord(
        tenant_id=tenant,
        subject="iso",
        resource="skills",
        action="read",
        effect="allow",
    )
    await get_policy_repository().save_policy(policy)
    return policy.id


async def _read_policy(tenant: str) -> set[str]:
    from forgeflow.repositories import get_policy_repository

    rows = await get_policy_repository().list_policies(tenant, limit=500)
    return {r.id for r in rows}


async def _write_cost(tenant: str) -> str:
    from forgeflow.repositories.factory import get_cost_repository

    # A unique (scope, scope_id) key — the upsert is keyed by it, so reusing one
    # would UPDATE (and keep the first row's id) rather than insert a new marker.
    budget = CostBudgetRecord(tenant_id=tenant, scope="task", scope_id=f"iso-{_suffix()}")
    await get_cost_repository().upsert(budget)
    return budget.id


async def _read_cost(tenant: str) -> set[str]:
    from forgeflow.repositories.factory import get_cost_repository

    rows = await get_cost_repository().list_by_scope(tenant)
    return {r.id for r in rows}


async def _write_eval_sample(tenant: str) -> str:
    from forgeflow.repositories.factory import get_eval_sample_repository

    sample = EvalSample(
        tenant_id=tenant,
        run_id=f"iso-{_suffix()}",
        metric_name="conformance",
        metric_value=1.0,
        dimension="quality",
    )
    await get_eval_sample_repository().save_sample(tenant, sample)
    return sample.id


async def _read_eval_sample(tenant: str) -> set[str]:
    from forgeflow.repositories.factory import get_eval_sample_repository

    rows = await get_eval_sample_repository().list_samples(tenant, limit=500)
    return {r.id for r in rows}


async def _write_listing(tenant: str) -> str:
    from forgeflow.repositories import get_skill_repository
    from forgeflow.skills.marketplace_bridge import publish_listing

    skill = SkillRecord(
        tenant_id=tenant,
        name=f"iso-{_suffix()}",
        domain="general",
        status="published",
        current_version="1.0.0",
    )
    await get_skill_repository().create_skill(skill)
    listing = await publish_listing(
        tenant, skill.id, actor="iso", actor_permissions=["*:*"]
    )
    return listing.id


async def _read_listing(tenant: str) -> set[str]:
    from forgeflow.skills.marketplace_bridge import search_listings

    rows = await search_listings(tenant, limit=500)
    return {r.id for r in rows}


#: (label, write, read) for every hub repository + the marketplace bridge.
PROBES: list[tuple[str, Writer, Reader]] = [
    ("experience", _write_experience, _read_experience),
    ("skill", _write_skill, _read_skill),
    ("candidate", _write_candidate, _read_candidate),
    ("policy", _write_policy, _read_policy),
    ("cost", _write_cost, _read_cost),
    ("eval_sample", _write_eval_sample, _read_eval_sample),
    ("marketplace", _write_listing, _read_listing),
]


async def _assert_pairwise_isolation(label: str, write: Writer, read: Reader) -> None:
    """Each tenant sees its own marker and none of the other two tenants'."""
    marker = {tenant: await write(tenant) for tenant in NON_UUID_TENANTS}
    for tenant in NON_UUID_TENANTS:
        visible = await read(tenant)
        assert marker[tenant] in visible, (
            f"[{label}] tenant {tenant!r} cannot read back its own row — "
            "isolation is too tight"
        )
        leaked = {
            other: marker[other]
            for other in NON_UUID_TENANTS
            if other != tenant and marker[other] in visible
        }
        assert not leaked, (
            f"[{label}] tenant {tenant!r} saw other tenants' rows {leaked!r} — "
            "cross-tenant leak (the retired UUID coercion collapses these "
            "non-UUID tenants onto one shared NULL bucket on PostgreSQL)"
        )


#: The hub tables whose ``tenant_id`` this file writes under the fixed trio.
_HUB_TABLES: tuple[str, ...] = (
    "experiences",
    "skills",
    "skill_candidates",
    "policies",
    "cost_budgets",
    "agent_eval_samples",
    "skill_listings",
)


def _cleanup_trio_on_postgres() -> None:
    """Best-effort removal of this file's fixed-tenant rows (postgres only).

    Keeps the shared dev database from accumulating conformance rows across
    runs. Membership-based assertions do not *need* this, so a failure is
    swallowed — cleanup must never turn a green case red.
    """
    if get_settings().storage_backend.lower() != "postgres":
        return
    import psycopg

    dsn = get_settings().postgres_sync_url.replace(
        "postgresql+psycopg://", "postgresql://"
    )
    try:
        with psycopg.connect(dsn, connect_timeout=4) as conn:
            with conn.cursor() as cur:
                for table in _HUB_TABLES:
                    cur.execute(
                        f"DELETE FROM {table} WHERE tenant_id = ANY(%s)",
                        (list(NON_UUID_TENANTS),),
                    )
            conn.commit()
    except Exception:  # noqa: BLE001 — cleanup is strictly best-effort
        return


def _cleanup_memory_stores() -> None:
    """Reset the in-process stores these cases wrote under the fixed trio.

    The offline profile keeps module-global dict stores, so the fixed trio —
    and, importantly, the ``"default"`` sentinel — would otherwise leak into
    later memory-profile tests that assert emptiness (e.g.
    ``test_agent_eval_api.py::test_offline_samples_endpoint_is_empty_list``,
    which reads the ``"default"`` partition). Each store exposes a ``clear_*``
    test hook, which is the house isolation idiom; calling them is the memory
    analogue of :func:`_cleanup_trio_on_postgres`. Membership-based assertions
    do not *need* this, so it must never turn a green case red.
    """
    try:
        from forgeflow.repositories.eval_sample_repo import clear_eval_sample_store
        from forgeflow.repositories.memory.cost_repo import clear_cost_budget_store
        from forgeflow.repositories.memory.experience_repo import clear_memory_store
        from forgeflow.repositories.memory.policy_repo import clear_policy_store
        from forgeflow.repositories.memory.skill_repo import clear_skill_store
        from forgeflow.skills.marketplace_bridge import reset_marketplace

        clear_memory_store()
        clear_cost_budget_store()
        clear_skill_store()
        clear_policy_store()
        clear_eval_sample_store()
        reset_marketplace()
    except Exception:  # noqa: BLE001 — cleanup is strictly best-effort
        return


# --------------------------------------------------------------------------- #
# the conformance cases — one per repository, run on both backends              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("active_backend", ["memory", "postgres"], indirect=True)
@pytest.mark.parametrize("label,write,read", PROBES, ids=[p[0] for p in PROBES])
async def test_hub_repository_isolates_non_uuid_tenants(
    active_backend, label: str, write: Writer, read: Reader
) -> None:
    try:
        await _assert_pairwise_isolation(label, write, read)
    finally:
        if active_backend == "postgres":
            _cleanup_trio_on_postgres()
        else:
            _cleanup_memory_stores()
