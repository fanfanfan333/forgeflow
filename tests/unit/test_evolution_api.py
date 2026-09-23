"""GET /skills/evolution-advice — rule-only under mock, approval-gated.

R1 budget rule: under ``LLM_PROVIDER=mock`` the endpoint must make **zero** LLM
calls and still return advice. The design verdict §7.5 requires every advice to
be presented as needing human approval (``approval_required: true``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from forgeflow.api.main import app
from forgeflow.api.routers.skills import (
    skill_evolution_advice,
    submit_evolution_advice_for_approval,
)
from forgeflow.auth.jwt import create_access_token
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.rbac.models import UserContext
from forgeflow.skills import evolution


@dataclass
class _Skill:
    id: str
    name: str = "skill"
    status: str = "published"
    usage_count: int = 0
    eval_score: float | None = None
    retire_suggested: bool = False
    last_used_at: datetime | None = None
    current_version: str = "0.1.0"


class _FakeSkillRepo:
    def __init__(self, skills: list[_Skill]) -> None:
        self._skills = skills

    async def list_skills(self, tenant_id, limit: int = 200, **kwargs):
        return list(self._skills), len(self._skills)


async def test_evolution_advice_is_rule_only_and_requires_approval(monkeypatch):
    """Mock provider ⇒ 0 LLM calls, advice still produced, approval flag set."""
    import forgeflow.models as models

    def _boom(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("LLM must not be called when LLM_PROVIDER=mock")

    monkeypatch.setattr(models, "get_model", _boom)
    monkeypatch.setattr(
        "forgeflow.repositories.get_skill_repository",
        lambda: _FakeSkillRepo(
            [
                _Skill(id="s-retire", name="旧技能", retire_suggested=True),
                _Skill(id="s-low", name="低分技能", eval_score=0.30, usage_count=9),
                _Skill(
                    id="s-ok",
                    name="健康技能",
                    eval_score=0.95,
                    usage_count=9,
                    last_used_at=datetime.now(timezone.utc),
                ),
            ]
        ),
    )
    evolution.reset_advice_cache()

    payload = await skill_evolution_advice(tenant="t-evo-api")

    assert payload["approval_required"] is True
    assert payload["tenant_id"] == "t-evo-api"
    # Only the two candidates (retire-suggested + low score) produce advice.
    assert payload["total"] == 2
    assert {item["skill_id"] for item in payload["items"]} == {"s-retire", "s-low"}
    assert {item["kind"] for item in payload["items"]} == {"retire", "optimize"}
    assert all(item["source"] == "rule" for item in payload["items"])
    # Every item names its approval kind so the queue can classify it.
    assert {item["approval_kind"] for item in payload["items"]} == {
        "skill.retire",
        "skill.optimize",
    }


async def test_evolution_advice_empty_when_no_candidates(monkeypatch):
    monkeypatch.setattr(
        "forgeflow.repositories.get_skill_repository",
        lambda: _FakeSkillRepo([_Skill(id="s-healthy", eval_score=0.99, usage_count=9)]),
    )
    evolution.reset_advice_cache()

    payload = await skill_evolution_advice(tenant="t-evo-empty")

    assert payload["approval_required"] is True
    assert payload["total"] == 0
    assert payload["items"] == []


# --------------------------------------------------------------------------- #
# POST /skills/evolution-advice/{skill_id}/approval — §7.5 wiring              #
# --------------------------------------------------------------------------- #

async def test_submit_evolution_advice_creates_pending_approval(monkeypatch):
    """The POST action persists a pending approval and never mutates the skill."""
    fake = _FakeSkillRepo(
        [_Skill(id="s-low", name="低分技能", eval_score=0.30, usage_count=9)]
    )
    monkeypatch.setattr("forgeflow.repositories.get_skill_repository", lambda: fake)
    evolution.reset_advice_cache()

    payload = await submit_evolution_advice_for_approval(
        skill_id="s-low",
        user=UserContext(user_id="admin-1", role="admin"),
        tenant="t-evo-approve",
    )

    assert payload["tenant_id"] == "t-evo-approve"
    assert payload["skill_id"] == "s-low"
    assert payload["created"] == 1
    approval = payload["approvals"][0]
    assert approval["kind"] == "skill.optimize"
    assert approval["status"] == "pending"
    assert approval["requested_action"] == "skill.optimize:s-low"
    assert approval["requester"] == "admin-1"
    # The skill body is untouched — the repo was read, never written.
    assert fake._skills[0].eval_score == 0.30
    assert fake._skills[0].status == "published"


async def test_submit_evolution_advice_404_when_no_advice(monkeypatch):
    monkeypatch.setattr(
        "forgeflow.repositories.get_skill_repository",
        lambda: _FakeSkillRepo([_Skill(id="s-healthy", eval_score=0.99, usage_count=9)]),
    )
    evolution.reset_advice_cache()

    with pytest.raises(HTTPException) as exc:
        await submit_evolution_advice_for_approval(
            skill_id="s-healthy",
            user=UserContext(user_id="admin-1", role="admin"),
            tenant="t-evo-none",
        )
    assert exc.value.status_code == 404


def test_evolution_approval_route_inherits_write_skills():
    # Longest-prefix match on the /skills prefix keeps UNMAPPED at 0.
    assert RBACMiddleware._resolve_permission(
        "POST", "/skills/evolution-advice/s-x/approval"
    ) == ("write", "skills")


def test_evolution_approval_route_over_http(monkeypatch):
    """Real HTTP wiring: 401 without a token, 201 with an admin token + an advice."""
    monkeypatch.setattr(
        "forgeflow.repositories.get_skill_repository",
        lambda: _FakeSkillRepo(
            [_Skill(id="s-low", name="低分技能", eval_score=0.30, usage_count=9)]
        ),
    )
    evolution.reset_advice_cache()

    client = TestClient(app)
    url = "/skills/evolution-advice/s-low/approval"
    assert client.post(url).status_code == 401

    token = create_access_token(user_id="admin-1", role="admin")
    resp = client.post(url, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["created"] == 1
    assert body["approvals"][0]["kind"] == "skill.optimize"
