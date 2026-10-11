"""INC46 T06 — independent QA (round-2) probes for the insights API.

This file is **additive**: it does not touch the frozen assertions of
``tests/integration/test_inc46_insights_api.py``. It attacks the seams the T06
DoD only *partially* pinned — chiefly the **unpinned cross-tenant WRITE**
(``POST /skills/forge`` with another tenant's ``experience_ids``) and the
same-name-skill read path. Every case runs against the **real** router behind
the **real** ``RBACMiddleware`` on the memory repositories, exactly like the
frozen suite.

It also carries two *characterisation* pins that document current behaviour so a
future refactor that changes it goes red (they are labelled as such).
"""

from __future__ import annotations
from tests.business_auth import authenticate_business_app

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
from forgeflow.skills.rule_assets import clear_rule_store, extract_rules, persist_rules

pytestmark = pytest.mark.asyncio

_TENANT = "t-qa2-t06"
_OTHER = "t-qa2-t06-other"
_MIN_EXP = 3


@pytest.fixture(autouse=True)
def _isolate(force_memory_backend):
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
    minimal = FastAPI()
    authenticate_business_app(minimal)
    minimal.add_middleware(RBACMiddleware)
    minimal.include_router(skill_insights.router, prefix="/skills")
    return TestClient(minimal)


def _auth(tenant: str = _TENANT, role: str = "admin") -> dict[str, str]:
    token = create_access_token(user_id="eng-qa2", role=role, workspace_id=tenant)
    return {"Authorization": f"Bearer {token}"}


async def _seed_skill(tenant: str, *, name: str = "技能") -> SkillRecord:
    repo = get_skill_repository()
    skill = SkillRecord(
        tenant_id=tenant, name=name, domain="general",
        status="published", current_version="1.0.0",
    )
    await repo.create_skill(skill)
    version = SkillVersionRecord(
        tenant_id=tenant, skill_id=skill.id, semver="1.0.0",
        spec={"tools": ["data.export"]}, eval_score=None,
        source_experience_ids=[],
    )
    await repo.add_version(tenant, version)
    return skill


async def _seed_experience(tenant: str, summary: str = "经验") -> ExperienceRecord:
    rec = ExperienceRecord(
        tenant_id=tenant, run_id="run-1", summary=summary,
        outcome="success", tags=["general"],
    )
    await get_experience_repository().save(rec)
    return rec


# --------------------------------------------------------------------------- #
# E1 — cross-tenant WRITE: A submits B's experience_ids to POST /skills/forge   #
# --------------------------------------------------------------------------- #
async def test_forge_write_does_not_ingest_foreign_experience_ids() -> None:
    """A manual Forge with a *foreign* id must not pull that row into A's record.

    The DoD only pinned the *read-back* 404. This pins the **write** side: the
    foreign id must resolve to nothing (repo is tenant-partitioned) so it is
    never echoed back into ``experience_ids`` nor persisted into A's ledger.
    """
    foreign = await _seed_experience(_OTHER, summary="他人经验-不应被吞")

    resp = _client().post(
        "/skills/forge", json={"mode": "manual", "experience_ids": [foreign.id]},
        headers=_auth(_TENANT),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "insufficient"
    # The foreign id must NOT be echoed anywhere.
    assert body["experience_ids"] == [], body
    assert foreign.id not in resp.text, "foreign experience id leaked into the response"

    # And the persisted ledger record is A's, unreadable by B.
    forge_id = body["forge_id"]
    assert _client().get(f"/skills/forge/{forge_id}", headers=_auth(_TENANT)).status_code == 200
    assert _client().get(f"/skills/forge/{forge_id}", headers=_auth(_OTHER)).status_code == 404


async def test_forge_write_mixed_ids_only_own_surfaces() -> None:
    """A mixed [owned, foreign] id list keeps only the owned id (no leak)."""
    owned = await _seed_experience(_TENANT, summary="本租户经验")
    foreign = await _seed_experience(_OTHER, summary="他人经验")

    body = _client().post(
        "/skills/forge",
        json={"mode": "manual", "experience_ids": [owned.id, foreign.id]},
        headers=_auth(_TENANT),
    ).json()
    assert body["status"] == "insufficient"  # 1 < 3
    assert body["experience_ids"] == [owned.id], body
    assert foreign.id not in str(body)


# --------------------------------------------------------------------------- #
# E2 — same-NAME skill living in another tenant is not readable by id           #
# --------------------------------------------------------------------------- #
async def test_same_name_skill_in_other_tenant_is_unreadable() -> None:
    """Identity is the skill **id**, not the name — a foreign id is an honest 404."""
    mine = await _seed_skill(_TENANT, name="同名技能")
    theirs = await _seed_skill(_OTHER, name="同名技能")
    assert mine.id != theirs.id

    # A can read its own id …
    assert _client().get(f"/skills/{mine.id}/rules", headers=_auth(_TENANT)).status_code == 200
    # … but the *other* tenant's identically-named skill is a 404, not a swap.
    for suffix in ("rules", "experience", "readiness"):
        r = _client().get(f"/skills/{theirs.id}/{suffix}", headers=_auth(_TENANT))
        assert r.status_code == 404, (suffix, r.text)


# --------------------------------------------------------------------------- #
# E3 — characterisation: the rules endpoint is TENANT-wide, not skill-scoped     #
# --------------------------------------------------------------------------- #
async def test_rules_endpoint_is_tenant_wide_id_is_only_an_ownership_gate() -> None:
    """Characterisation pin (documents current behaviour, not a DoD claim).

    ``GET /skills/{id}/rules`` gates on *ownership of {id}* but returns the
    **tenant's** rule set (``list_rules(tenant)``) — so two distinct owned skills
    see the *same* rules. This is the declared contract (module docstring says
    "the tenant's MUST / MUST_NOT rules"); the pin exists so a refactor to
    per-skill filtering is a visible, reviewed decision.
    """
    rules = extract_rules([], [
        [{"tool": "research.search", "status": "ok", "run_id": "r1", "step_index": 0},
         {"tool": "report.render", "status": "ok", "run_id": "r1", "step_index": 1}],
        [{"tool": "research.search", "status": "ok", "run_id": "r2", "step_index": 0},
         {"tool": "report.render", "status": "ok", "run_id": "r2", "step_index": 1}],
        # 夹具缺陷修复（主理人代修，QA 200s 后因 429 中断）：原夹具只种了成功 run，
        # 抽不出 must_not 规则，却断言 must_not 非空 ⇒ 属测试自身缺陷，非产品缺陷。
        # 补两条失败 run，使 must / must_not 两条都真实存在，特征钉才有载荷。
        [{"tool": "data.export", "status": "error", "run_id": "r3", "step_index": 0}],
        [{"tool": "data.export", "status": "error", "run_id": "r4", "step_index": 0}],
    ], min_support=2, min_confidence=0.8)
    assert await persist_rules(_TENANT, rules)

    a = await _seed_skill(_TENANT, name="技能A")
    b = await _seed_skill(_TENANT, name="技能B")
    body_a = _client().get(f"/skills/{a.id}/rules", headers=_auth(_TENANT)).json()
    body_b = _client().get(f"/skills/{b.id}/rules", headers=_auth(_TENANT)).json()

    assert body_a["must"] and body_a["must_not"]
    # Same tenant ⇒ identical rule set regardless of which owned skill_id is used.
    assert body_a["must"] == body_b["must"]
    assert body_a["must_not"] == body_b["must_not"]
    # The id is echoed, so the surface is still attributable to the requested skill.
    assert body_a["skill_id"] == a.id and body_b["skill_id"] == b.id


# --------------------------------------------------------------------------- #
# A-strengthening — the cross-tenant 404 is caused by TENANT scoping            #
# --------------------------------------------------------------------------- #
async def test_cross_tenant_404_is_not_mere_absence() -> None:
    """A KNOWN foreign id is still 404 for the wrong tenant (isolates the gate).

    ``test_unknown_skill_is_404`` only proves an *absent* id is 404. This seeds a
    **real** row under another tenant and proves the 404 comes from the tenant
    predicate (the row exists), not from an empty store.
    """
    theirs = await _seed_skill(_OTHER, name="他人技能")
    repo = get_skill_repository()
    # Sanity: the row really exists under its owner.
    assert await repo.get_skill(_OTHER, theirs.id) is not None
    assert await repo.get_skill(_TENANT, theirs.id) is None
    r = _client().get(f"/skills/{theirs.id}/rules", headers=_auth(_TENANT))
    assert r.status_code == 404, r.text
