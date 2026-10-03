"""INC44 T01 (pg-档) — loop → publish closure on **real PostgreSQL**.

The design's T01 acceptance criterion ⑥ asks the closure to hold on **both**
profiles (``memory`` and ``postgres``) — the loop's fix is a persistence side
effect, so a backend-neutral pin matters. The memory-档 companion lives in
``tests/integration/test_inc44_skill_publish.py``; this file re-proves the same
closed loop through real ``asyncpg`` against the live dev Postgres.

It is **zero-migration**: it reuses the existing ``skill_evaluations`` table
(migration backlog is untouched — the head stays where it is) and skips cleanly
when no Postgres is reachable, exactly like ``tests/integration/test_agent_eval_pg.py``.

What it proves that a mocked pool cannot:

  * ``run_engineering_loop`` → ``PgSkillCandidateRepository.save_evaluation``
    actually **writes** the passing evaluation (verdict ``pass``, ``metrics``
    keyed on ``tester.structural_score``) and the status write-back lands;
  * the unmodified ``governance_gate.promote_candidate`` then reads that same
    row back through real SQL and mints a ``0.1.0`` version whose ``eval_score``
    carries the loop's measured score.
"""

from __future__ import annotations

import pathlib
import socket
import subprocess
import sys
import uuid

import asyncpg
import pytest

from forgeflow.config import get_settings
from forgeflow.repositories.postgres.policy_repo import PgPolicyRepository
from forgeflow.repositories.postgres.skill_repo import (
    PgSkillCandidateRepository,
    PgSkillRepository,
)
from forgeflow.skills import engineering as eng
from forgeflow.skills.models import SkillCandidateRecord
from forgeflow.skills.tester import structural_score

# Captured at import (before conftest's autouse DNS stub) so we reach a real
# 127.0.0.1 rather than the stubbed public IP.
_REAL_GETADDRINFO = socket.getaddrinfo
_DSN = get_settings().postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")

_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: A complete draft (four gating elements present, tools all whitelisted) ⇒ the
#: critique is clean and the sandbox passes, so the loop reaches the success exit.
_COMPLETE_DRAFT = {
    "prompt": "分析客户流失并输出挽留建议",
    "steps": ["拉取客户行为数据", "计算流失概率", "生成挽留建议"],
    "tools": ["data.query", "analysis.score", "report.render"],
    "io_schema": {"input": {"intent": "string"}, "output": {"summary": "string"}},
    "applicable_when": {"domain": "数据分析"},
}


@pytest.fixture
async def pool(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    # Mirror the production pool's per-connection setup: register the JSON/JSONB
    # codec so the dicts in ``draft_spec``/``metrics`` bind into the JSONB columns
    # and read back as dicts. Without it asyncpg raises 'expected str, got dict'
    # (the same reason the app installs it via ``init_pool(init=_init_connection)``).
    from forgeflow.database import _init_connection

    try:
        p = await asyncpg.create_pool(
            _DSN, min_size=1, max_size=3, timeout=4, init=_init_connection
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live Postgres not reachable ({exc})")
    yield p
    await p.close()


def _upgrade_head() -> None:
    """Run ``alembic upgrade head`` in a **subprocess** (zero new migration).

    A subprocess is deliberate (same rationale as ``test_agent_eval_pg.py``):
    alembic's ``env.py`` reconfigures logging, which would silence loggers this
    process already created and break a later ``caplog`` test. Isolating it keeps
    the test session's logging intact. No migration is added by INC44, so this is
    a pure idempotent catch-up against the on-disk head.
    """
    import os

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


async def test_loop_then_publish_closes_the_skill_lifecycle_on_postgres(pool):
    """synthesize-shaped candidate → run_engineering_loop → version_and_publish."""
    _upgrade_head()
    tenant = f"t-inc44pg-{uuid.uuid4().hex[:8]}"
    cand_repo = PgSkillCandidateRepository(pool=pool)
    skill_repo = PgSkillRepository(pool=pool)
    pol_repo = PgPolicyRepository(pool=pool)

    candidate = SkillCandidateRecord(
        tenant_id=tenant,
        name=f"候选 {uuid.uuid4().hex[:6]}",
        domain="数据分析",
        # ``skill_candidates.experience_ids`` is a real ``UUID[]`` column, so the
        # ids must be UUIDs (unlike the memory profile, which stores any string).
        experience_ids=[str(uuid.uuid4()) for _ in range(3)],
        draft_spec=dict(_COMPLETE_DRAFT),
        status="draft",
    )

    try:
        await cand_repo.save_candidate(candidate)

        # ④→⑤→⑥→⑦ — the loop now persists the evaluation it actually ran.
        result = await eng.run_engineering_loop(
            tenant,
            candidate.id,
            "manager-1",
            "manager",
            candidate_repo=cand_repo,
            skill_repo=skill_repo,
            policy_repo=pol_repo,
        )
        assert result.passed is True
        assert result.lifecycle == "REVIEW"

        # The passing evaluation round-tripped through real SQL ...
        evaluation = await cand_repo.get_evaluation_for(tenant, candidate.id)
        assert evaluation is not None, "the loop must persist the evaluation it ran"
        assert evaluation.verdict == "pass"
        # ... keyed on the one yardstick (never a fabricated score).
        assert evaluation.metrics["score"] == structural_score(result.contract)

        # ... and the status write-back landed (approved ⇔ REVIEW).
        stored = await cand_repo.get_candidate(tenant, candidate.id)
        assert stored is not None
        assert stored.status == "approved"

        # ⑦ — the *unmodified* publish gate now finds the stored pass ⇒ no 403.
        version = await eng.version_and_publish(
            tenant,
            candidate.id,
            "manager-1",
            "manager",
            candidate_repo=cand_repo,
            skill_repo=skill_repo,
            policy_repo=pol_repo,
        )
        parts = version.semver.split(".")
        assert len(parts) == 3 and all(p.isdigit() for p in parts)
        assert version.semver == "0.1.0"
        assert version.approved_by == "manager-1"
        assert version.eval_score is not None

        promoted = await cand_repo.get_candidate(tenant, candidate.id)
        assert promoted.status == "promoted"
    finally:
        # Clean up every row this test owns, by the opaque tenant string.
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM skill_versions WHERE tenant_id = $1", tenant)
            await conn.execute("DELETE FROM skills WHERE tenant_id = $1", tenant)
            await conn.execute(
                "DELETE FROM skill_evaluations WHERE tenant_id = $1", tenant
            )
            await conn.execute(
                "DELETE FROM skill_candidates WHERE tenant_id = $1", tenant
            )
