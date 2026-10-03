"""INC46 T06 — Skill *insights* API (rules / experience / readiness / forge).

End-to-end pins over the **real** router ``api/routers/skill_insights.py`` behind
the **real** ``RBACMiddleware`` and the **memory** repositories — no handler is
stubbed, no seam is monkeypatched (only the storage backend is pinned to memory,
exactly as the rest of the suite does).

What this file fixes (T06 DoD §五)
----------------------------------
阳性 (positive):
  * a normal skill's ``GET /skills/{id}/rules`` returns the tenant's real
    ``must`` / ``must_not`` rules, each with its recomputable ``support`` /
    ``confidence`` **and** the platform's *real* enforcement verdict
    (a ``must_not`` whose tool is in ``DANGEROUS_TOOLS()`` ⇒ ``enforced=True``;
    an ordering ``must`` ⇒ advisory);
  * the enforcement summary is derived from the single truth
    (``forgeflow.skills.tool_permissions``) — ``blocked_tools`` is the
    intersection of the declared spec tools with ``DANGEROUS_TOOLS()``.

阴性 (negative / honesty 红线 4 + 5):
  * **unmeasured ⇒ ``rate is None`` and ``evaluated == 0``** — the route never
    writes ``0`` for "not measured" (a measured ``0.0`` stays honest ``0.0``);
  * every cross-tenant read of ``rules`` / ``experience`` / ``readiness`` is an
    honest ``404`` (never another tenant's row);
  * ``GET /skills/forge/{foreign_id}`` is ``404`` (红线 5);
  * an under-privileged role (``viewer``) gets ``403`` on the Forge write, and
    an unauthenticated call gets ``401`` (fail-closed).

反事实 (counterfactual):
  * :func:`test_cross_tenant_forge_readback_is_404` is the documented red-proof
    target: deleting the tenant predicate in ``skill_insights._forge_record``
    (the ``str(record.get("tenant_id", "")) != tenant`` comparison) turns it red.

No new DB table is touched; the routes consume only pre-existing storage
(``skill_rule_suggestions`` / ``skills`` / ``skill_versions`` / ``experiences``).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.routers import skill_insights
from forgeflow.api.routers.skill_insights import reset_forge_registry
from forgeflow.auth.jwt import create_access_token
from forgeflow.experience.models import ExperienceRecord
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.repositories import get_experience_repository, get_skill_repository
from forgeflow.repositories.memory.experience_repo import clear_memory_store
from forgeflow.repositories.memory.skill_repo import clear_skill_store
from forgeflow.skills.models import SkillRecord, SkillVersionRecord
from forgeflow.skills.rule_assets import (
    clear_rule_store,
    extract_rules,
    persist_rules,
)
from forgeflow.skills.tool_permissions import DANGEROUS_TOOLS

pytestmark = pytest.mark.asyncio

_TENANT = "t-inc46-t06"
_OTHER = "t-inc46-t06-other"
_MIN_EXP = 3  # Settings.skill_candidate_min_experiences default (pinned below too)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate(force_memory_backend):
    """Pin the memory backend and wipe every in-process store per test.

    ``force_memory_backend`` drops the repository caches; the module-level dict
    stores (skills / experiences / rules / forge ledger) are separate singletons,
    so they are cleared here too — otherwise a prior test's rows would leak.
    """
    clear_skill_store()
    clear_memory_store()
    clear_rule_store()
    reset_forge_registry()
    yield
    clear_skill_store()
    clear_memory_store()
    clear_rule_store()
    reset_forge_registry()


def _client() -> TestClient:
    """The real insights router behind the real RBAC middleware (no stubs)."""
    minimal = FastAPI()
    minimal.add_middleware(RBACMiddleware)
    minimal.include_router(skill_insights.router, prefix="/skills")
    return TestClient(minimal)


def _auth(tenant: str = _TENANT, role: str = "admin") -> dict[str, str]:
    token = create_access_token(user_id="eng-t06", role=role, workspace_id=tenant)
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Seeding helpers                                                              #
# --------------------------------------------------------------------------- #
async def _seed_skill(
    tenant: str = _TENANT,
    *,
    eval_score: float | None = None,
    spec: dict | None = None,
    source_experience_ids: list[str] | None = None,
    name: str = "演示技能",
) -> tuple[SkillRecord, SkillVersionRecord]:
    """Create a published skill with one ``1.0.0`` version (memory repos)."""
    repo = get_skill_repository()
    skill = SkillRecord(
        tenant_id=tenant,
        name=name,
        domain="general",
        status="published",
        current_version="1.0.0",
    )
    await repo.create_skill(skill)
    version = SkillVersionRecord(
        tenant_id=tenant,
        skill_id=skill.id,
        semver="1.0.0",
        spec=dict(spec or {}),
        eval_score=eval_score,
        source_experience_ids=list(source_experience_ids or []),
    )
    await repo.add_version(tenant, version)
    return skill, version


async def _seed_experience(
    tenant: str, *, summary: str = "一条经验", tags: list[str] | None = None
) -> ExperienceRecord:
    record = ExperienceRecord(
        tenant_id=tenant,
        run_id="run-1",
        summary=summary,
        outcome="success",
        tags=list(tags or ["general"]),
    )
    await get_experience_repository().save(record)
    return record


def _step(tool: str, *, status: str, run_id: str, index: int) -> dict:
    return {"tool": tool, "status": status, "run_id": run_id, "step_index": index}


def _evidence_runs() -> list[list[dict]]:
    """Two successful runs (``research.search``→``report.render``) + two failing
    ``data.export`` runs — enough to emit exactly one ``must`` and one ``must_not``.
    """
    ok_1 = [_step("research.search", status="ok", run_id="r1", index=0),
            _step("report.render", status="ok", run_id="r1", index=1)]
    ok_2 = [_step("research.search", status="ok", run_id="r2", index=0),
            _step("report.render", status="ok", run_id="r2", index=1)]
    bad_1 = [_step("data.export", status="error", run_id="r3", index=0)]
    bad_2 = [_step("data.export", status="error", run_id="r4", index=0)]
    return [ok_1, ok_2, bad_1, bad_2]


async def _seed_rules(tenant: str = _TENANT) -> None:
    rules = extract_rules([], _evidence_runs(), min_support=2, min_confidence=0.8)
    assert await persist_rules(tenant, rules), "rules must persist for a real tenant"


# --------------------------------------------------------------------------- #
# 阳性 — rules: real must / must_not + real enforcement                        #
# --------------------------------------------------------------------------- #
async def test_rules_returns_must_and_must_not_with_real_enforcement() -> None:
    spec = {"tools": ["data.export", "research.search", "report.render"]}
    skill, _ = await _seed_skill(spec=spec)
    await _seed_rules()

    response = _client().get(f"/skills/{skill.id}/rules", headers=_auth())
    assert response.status_code == 200, response.text
    body = response.json()

    # --- must: the discovered ordering rule, advisory (platform does not order) #
    assert len(body["must"]) == 1, body["must"]
    must_rule = body["must"][0]
    assert must_rule["rule_kind"] == "must"
    assert must_rule["support"] == 2 and must_rule["confidence"] == 1.0
    assert must_rule["enforced"] is False
    assert "建议" in must_rule["enforcement"]

    # --- must_not: enforced because its tool is genuinely DANGEROUS ------------- #
    assert len(body["must_not"]) == 1, body["must_not"]
    not_rule = body["must_not"][0]
    assert not_rule["rule_kind"] == "must_not"
    assert "data.export" in not_rule["rule_text"]
    assert not_rule["support"] == 2 and not_rule["confidence"] == 1.0
    assert not_rule["enforced"] is True, not_rule
    assert "data.export" in not_rule["enforcement"]

    # --- enforcement summary: derived from the single truth, no parallel table -- #
    enforcement = body["enforcement"]
    assert enforcement["source"] == "forgeflow.skills.tool_permissions"
    assert enforcement["declared_tools"] == [
        "data.export",
        "report.render",
        "research.search",
    ]
    assert enforcement["blocked_tools"] == ["data.export"]
    assert enforcement["risk_level"] == "high"
    assert enforcement["tool_classes"]["data.export"] == "DANGEROUS"
    # The forced-dangerous id proves this reads DANGEROUS_TOOLS(), not a subset.
    assert "code.commit" in enforcement["dangerous_tools"]
    assert sorted(enforcement["dangerous_tools"]) == sorted(DANGEROUS_TOOLS())


async def test_rules_is_empty_honestly_when_none_extracted() -> None:
    """No rules ⇒ real empty lists (no fabricated rule)."""
    skill, _ = await _seed_skill()
    response = _client().get(f"/skills/{skill.id}/rules", headers=_auth())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["must"] == [] and body["must_not"] == []
    assert body["enforcement"]["declared_tools"] == []
    assert body["enforcement"]["blocked_tools"] == []


# --------------------------------------------------------------------------- #
# 阴性 — readiness honesty (红线 4): unmeasured ⇒ None, never 0                 #
# --------------------------------------------------------------------------- #
async def test_readiness_unmeasured_rate_is_none_never_zero() -> None:
    skill, _ = await _seed_skill(eval_score=None)
    response = _client().get(f"/skills/{skill.id}/readiness", headers=_auth())
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["rate"] is None, "未测量必须为 None（绝不写 0）"
    assert body["evaluated"] == 0
    assert body["has_version"] is True
    # The consumer distinguishes unmeasured from measured-zero via ``evaluated``.
    ev_check = next(c for c in body["checks"] if c["name"] == "has_evaluation")
    assert ev_check["ok"] is False
    assert "None" in ev_check["evidence"] or "未测量" in ev_check["evidence"]


async def test_readiness_measured_rate_is_the_real_score() -> None:
    """A measured score round-trips (and a measured ``0.0`` is an honest ``0.0``)."""
    skill, _ = await _seed_skill(eval_score=0.0)
    response = _client().get(f"/skills/{skill.id}/readiness", headers=_auth())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rate"] == 0.0, "测得为 0 == 如实汇报 0（≠ 未测量）"
    assert body["evaluated"] == 1

    skill2, _ = await _seed_skill(eval_score=0.75, name="另一个技能")
    body2 = _client().get(f"/skills/{skill2.id}/readiness", headers=_auth()).json()
    assert body2["rate"] == 0.75 and body2["evaluated"] == 1


# --------------------------------------------------------------------------- #
# 阳性 / 阴性 — experience provenance                                          #
# --------------------------------------------------------------------------- #
async def test_experience_returns_only_owned_source_experiences() -> None:
    owned = await _seed_experience(_TENANT, summary="本租户经验")
    foreign = await _seed_experience(_OTHER, summary="他人经验")
    # The version's provenance names both ids; only the owned one may surface.
    skill, _ = await _seed_skill(source_experience_ids=[owned.id, foreign.id])

    response = _client().get(f"/skills/{skill.id}/experience", headers=_auth())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1, body
    assert [item["id"] for item in body["items"]] == [owned.id]
    assert body["items"][0]["summary"] == "本租户经验"


async def test_experience_is_empty_without_version() -> None:
    """A skill with no current version has no provenance ⇒ empty, not fabricated."""
    from forgeflow.repositories import get_skill_repository

    repo = get_skill_repository()
    skill = SkillRecord(tenant_id=_TENANT, name="无版本技能", current_version=None)
    await repo.create_skill(skill)
    response = _client().get(f"/skills/{skill.id}/experience", headers=_auth())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 0 and body["items"] == []


# --------------------------------------------------------------------------- #
# 阴性 — cross-tenant reads are honest 404 (红线 5)                            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("suffix", ["rules", "experience", "readiness"])
async def test_cross_tenant_skill_reads_are_404(suffix: str) -> None:
    skill, _ = await _seed_skill(_TENANT)
    response = _client().get(f"/skills/{skill.id}/{suffix}", headers=_auth(_OTHER))
    assert response.status_code == 404, response.text


async def test_unknown_skill_is_404() -> None:
    response = _client().get("/skills/does-not-exist/rules", headers=_auth())
    assert response.status_code == 404, response.text


# --------------------------------------------------------------------------- #
# Forge — insufficient (honest) + compiled (real)                              #
# --------------------------------------------------------------------------- #
async def test_forge_reports_insufficient_with_the_real_threshold() -> None:
    exp = await _seed_experience(_TENANT)
    response = _client().post(
        "/skills/forge",
        json={"mode": "manual", "experience_ids": [exp.id]},
        headers=_auth(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "insufficient"
    assert body["required_experiences"] == _MIN_EXP
    assert body["experience_ids"] == [exp.id]
    assert body["candidate_id"] == ""
    assert body["forge_id"].startswith("forge-")


async def test_forge_reports_insufficient_with_no_experiences() -> None:
    response = _client().post("/skills/forge", json={"mode": "auto"}, headers=_auth())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "insufficient"
    assert body["required_experiences"] == _MIN_EXP
    assert body["experience_ids"] == []


async def test_forge_compiles_a_candidate_with_enough_experiences() -> None:
    exps = [await _seed_experience(_TENANT, summary=f"经验 {i}") for i in range(_MIN_EXP)]
    ids = [e.id for e in exps]

    response = _client().post(
        "/skills/forge",
        json={"mode": "manual", "experience_ids": ids},
        headers=_auth(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "compiled", body
    assert body["candidate_id"], "a compiled candidate must carry its id"
    assert body["required_experiences"] is None
    assert set(body["experience_ids"]) == set(ids)

    # Read-back round-trips through the same tenant.
    forge_id = body["forge_id"]
    read = _client().get(f"/skills/forge/{forge_id}", headers=_auth())
    assert read.status_code == 200, read.text
    assert read.json() == body


# --------------------------------------------------------------------------- #
# 反事实目标 — cross-tenant Forge read-back is 404 (删此谓词 ⇒ 本用例转红)      #
# --------------------------------------------------------------------------- #
async def test_cross_tenant_forge_readback_is_404() -> None:
    """**Counterfactual target.** Deleting the tenant predicate in
    ``skill_insights._forge_record`` (``str(record.get("tenant_id", "")) != tenant``)
    makes this assertion fail (the foreign read-back would return 200).

    Control: the owner reads its own record back successfully.
    """
    exp = await _seed_experience(_TENANT)
    forged = _client().post(
        "/skills/forge",
        json={"mode": "manual", "experience_ids": [exp.id]},
        headers=_auth(_TENANT),
    ).json()
    forge_id = forged["forge_id"]

    # Control — the owner sees it.
    own = _client().get(f"/skills/forge/{forge_id}", headers=_auth(_TENANT))
    assert own.status_code == 200, own.text

    # The load-bearing isolation gate — a foreign tenant must NOT.
    foreign = _client().get(f"/skills/forge/{forge_id}", headers=_auth(_OTHER))
    assert foreign.status_code == 404, foreign.text


# --------------------------------------------------------------------------- #
# 阴性 — fail-closed auth / RBAC                                               #
# --------------------------------------------------------------------------- #
async def test_forge_requires_write_skills() -> None:
    """``viewer`` holds ``read:skills`` but not ``write:skills`` ⇒ 403."""
    response = _client().post("/skills/forge", json={"mode": "auto"}, headers=_auth(role="viewer"))
    assert response.status_code == 403, response.text


async def test_unauthenticated_is_401() -> None:
    skill, _ = await _seed_skill()
    response = _client().get(f"/skills/{skill.id}/rules")
    assert response.status_code == 401, response.text
