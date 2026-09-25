"""INC12-A6 — non-UUID ``run_id`` round-trips verbatim (memory ⇄ PostgreSQL).

A2 removed the seven copies of ``_as_uuid`` but left one shared successor,
``repositories.base.uuid_or_none``, on three columns that were still ``UUID``:
``experiences.run_id``, ``agent_approvals.run_id`` and
``context_build_stats.run_id``. It is the same silent-NULL defect A2 removed for
``tenant_id``, one column over, and it has two faces:

  * **write** — a non-UUID hub run id (``"hub-abc"``) was coerced to ``NULL``,
    with no log and no exception, so the row lost its linkage to the run;
  * **read** — ``list(run_id="hub-abc")`` then rendered ``run_id = NULL``, a
    predicate that is never true in SQL, so the caller got an empty list and
    could not tell "no data" apart from "the linkage was thrown away on write".

Migration ``014`` widens those columns to opaque ``TEXT`` and the repositories
now pass the string through. This file pins the resulting contract on **both**
backends:

  * a non-UUID run id is stored **verbatim** and read back **verbatim**;
  * two different non-UUID run ids never read each other's rows.

Each case runs twice (parametrised ``memory`` / ``postgres``), and each parameter
**declares** its own profile instead of inheriting it from the environment — the
memory parameter through ``conftest.force_memory_backend``, the postgres one by
pinning ``storage_backend`` and migrating the live dev database to ``head``. That
is the discipline the project learned the hard way: a storage-dependent test that
inherited its profile eventually disagreed between the two profiles with nobody
alerted (the cross-tenant ``NULL`` collapse A2 fixed).

Re-runnability: every marker carries a fresh uuid4 suffix, so repeated runs add
rows instead of clobbering them, and assertions are membership-based on the ids
created in *this* run — leftover rows from other suites can never make them
vacuously green.

Non-vacuity: re-introducing ``uuid_or_none`` on either the write or the read path
makes the ``postgres`` parameter fail (a ``NULL`` linkage reads back as ``""``,
and the ``run_id =`` predicate matches nothing).
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
from forgeflow.governance.models import ApprovalRecord

pytestmark = pytest.mark.asyncio

_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Deliberately **not** UUIDs: exactly the shape the retired coercion collapsed
#: to ``NULL``. A hub run id is an opaque string, never a ``workflow_runs`` UUID.
_RUN_A = "hub-abc-123"
_RUN_B = "hub-def-456"
#: A third non-UUID run id that is never written — proves the read path answers
#: "nothing here" rather than "everything".
_RUN_NONE = "hub-nothing-999"


def _suffix() -> str:
    """Fresh marker suffix so the same case can be re-run without clobbering."""
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- #
# profile activation (mirrors tests/integration/test_tenant_isolation_          #
# conformance.py: each backend declares its own profile)                        #
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
    """Apply migrations to ``head`` in a subprocess (mirrors the PG suites)."""
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
        pytest.skip("live Postgres not reachable — run_id TEXT case needs it")
    try:
        _upgrade_head()
    except Exception as exc:  # noqa: BLE001 — an unusable DB means "skip here"
        pytest.skip(f"dev DB could not be migrated to head: {exc}")


@pytest.fixture
def active_backend(request, monkeypatch):
    """Activate the parametrised storage backend for one case.

    ``memory`` **declares** its profile through ``force_memory_backend`` (never
    inherits it). ``postgres`` requires a reachable, migrated database — migration
    ``014`` is what widens the columns — and pins ``storage_backend``, so a single
    ``pytest`` invocation exercises both profiles.
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
# cases                                                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("active_backend", ["memory", "postgres"], indirect=True)
async def test_non_uuid_run_id_roundtrips_verbatim(active_backend):
    """A hub run id survives the write and comes back byte-for-byte.

    ``postgres`` is the discriminating parameter: before ``014`` the column was
    ``UUID``, so ``"hub-abc-123"`` was coerced to ``NULL`` and read back as the
    empty string — the linkage was gone with no error anywhere.
    """
    from forgeflow.repositories import get_experience_repository

    suffix = _suffix()
    tenant = f"t-inc12-a6-{suffix}"
    run_a = f"{_RUN_A}-{suffix}"
    run_b = f"{_RUN_B}-{suffix}"

    repo = get_experience_repository()
    rec_a = ExperienceRecord(
        tenant_id=tenant, run_id=run_a, summary=f"a6-a-{suffix}", outcome="success"
    )
    rec_b = ExperienceRecord(
        tenant_id=tenant, run_id=run_b, summary=f"a6-b-{suffix}", outcome="success"
    )
    await repo.save(rec_a)
    await repo.save(rec_b)

    # 1. verbatim round-trip — the linkage is not silently dropped.
    fetched = await repo.get(tenant, rec_a.id)
    assert fetched is not None
    assert fetched.run_id == run_a

    # 2. the run_id filter matches the row that really carries it …
    rows_a = await repo.list(tenant, run_id=run_a, limit=500)
    assert rec_a.id in {r.id for r in rows_a}
    # … and never the one carrying a different non-UUID id.
    assert rec_b.id not in {r.id for r in rows_a}
    assert {r.run_id for r in rows_a if r.id == rec_a.id} == {run_a}

    # 3. the mirror image: run_b reads its own row and not the other's.
    rows_b = await repo.list(tenant, run_id=run_b, limit=500)
    assert rec_b.id in {r.id for r in rows_b}
    assert rec_a.id not in {r.id for r in rows_b}

    # 4. an id that was never written is honestly empty — the fix must not turn
    #    "no match" into "everything matches".
    assert await repo.list(tenant, run_id=f"{_RUN_NONE}-{suffix}", limit=500) == []


@pytest.mark.parametrize("active_backend", ["memory", "postgres"], indirect=True)
async def test_non_uuid_run_id_on_approvals_roundtrips(active_backend):
    """Same contract on ``agent_approvals.run_id`` (the HITL linkage column)."""
    from forgeflow.repositories import get_policy_repository

    suffix = _suffix()
    tenant = f"t-inc12-a6-{suffix}"
    run_a = f"{_RUN_A}-{suffix}"
    run_b = f"{_RUN_B}-{suffix}"

    repo = get_policy_repository()
    first = ApprovalRecord(
        tenant_id=tenant,
        run_id=run_a,
        risk_level="low",
        requested_action=f"a6-approve-{suffix}",
        status="pending",
    )
    second = ApprovalRecord(
        tenant_id=tenant,
        run_id=run_b,
        risk_level="low",
        requested_action=f"a6-approve-{suffix}",
        status="pending",
    )
    await repo.save_approval(first)
    await repo.save_approval(second)

    stored = await repo.get_approval(tenant, first.id)
    assert stored is not None
    assert stored.run_id == run_a

    stored_b = await repo.get_approval(tenant, second.id)
    assert stored_b is not None
    assert stored_b.run_id == run_b
    # Two distinct non-UUID run ids must not collapse onto one another.
    assert stored.run_id != stored_b.run_id


async def test_uuid_or_none_is_gone():
    """A6 deleted the helper outright — no repository may reintroduce it.

    Kept as a source-level assertion so a "temporary" re-add cannot slip back in
    through a refactor: ``grep uuid_or_none`` must find no live symbol.
    """
    import forgeflow.repositories.base as base

    assert not hasattr(base, "uuid_or_none")
    assert "uuid_or_none" not in base.__all__
