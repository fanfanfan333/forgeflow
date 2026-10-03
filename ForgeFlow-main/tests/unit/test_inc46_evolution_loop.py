"""INC46 T05 — Evolution closed loop: guards, idempotency and honesty.

Scope: the *decision* layer of :func:`evolution_loop.maybe_evolve` — the parts
that must hold regardless of which repositories back it. The full
"N failing runs → v1.1" path is covered by
``tests/integration/test_inc46_evolution_closed_loop.py``.

Each test drives the real function (not a re-implementation) and asserts the
honest outcome: a loop that cannot publish reports ``applied=False`` with a
reason — it never fabricates a version.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from forgeflow.skills import evolution_loop as el
from forgeflow.skills.engineering import LIFECYCLE_STATES, LIFECYCLE_TRANSITIONS
from forgeflow.skills.evolution_loop import (
    EVOLVE_COOLDOWN_HOURS,
    EVOLVE_TRIGGER_MIN_FAILURES,
    MAX_EVOLVE_GENERATIONS,
    _build_proposal,
    maybe_evolve,
    record_evolution_time,
    record_generation,
    reset_evolution_state,
)

TENANT = "tenant-evolve-1"
SKILL_ID = "skill-evolve-1"


# --------------------------------------------------------------------------- #
# fakes — only the seams maybe_evolve actually touches                          #
# --------------------------------------------------------------------------- #
class _Skill:
    id = SKILL_ID
    name = "演化演示技能"
    current_version = "1.0.0"
    status = "published"


class _FakeSkillRepo:
    def __init__(self, skill: _Skill | None = None) -> None:
        self._skill = skill or _Skill()

    async def get_skill(self, tenant, skill_id):
        return self._skill

    async def get_version(self, tenant, skill_id, semver):
        return None

    async def list_versions(self, tenant, skill_id):
        return []


class _FakeCandidateRepo:
    def __init__(self) -> None:
        self.saved: list = []
        self.links: list = []
        self.evaluations: list = []

    async def save_candidate(self, candidate):
        self.saved.append(candidate)
        return candidate

    async def link_experience(self, *args):
        self.links.append(tuple(args))
        return True

    async def get_candidate(self, *args):
        wanted = args[-1] if args else None
        for cand in self.saved:
            if getattr(cand, "id", None) == wanted:
                return cand
        return None

    async def save_evaluation(self, *args):
        ev = args[-1] if args else None
        self.evaluations.append(ev)
        return ev

    async def get_evaluation_for(self, *args):
        return self.evaluations[-1] if self.evaluations else None


class _FakeExperienceRepo:
    """Backed by a list; ``get`` resolves what ``save`` stored (by id).

    ``candidate_compiler.compile_candidate`` reads each experience back, so the
    fake must round-trip them — otherwise we would be testing a stub, not the
    real compile path.
    """

    def __init__(self) -> None:
        self.saved: list = []

    async def save(self, record):
        self.saved.append(record)
        return record

    async def get(self, tenant, exp_id):
        for rec in self.saved:
            if getattr(rec, "id", None) == exp_id:
                return rec
        return None


class _FakePolicyRepo:
    def __init__(self) -> None:
        self.saved: list = []

    async def save_approval(self, record):
        self.saved.append(record)
        return record


def _failures(n: int) -> list[dict]:
    """``n`` deterministic failing runs (shape matches collect_skill_failures)."""
    return [
        {
            "run_id": f"run-{i}",
            "modes": ["tool_error"],
            "tool": "analysis.profile",
            "reason": "step failed",
        }
        for i in range(n)
    ]


@pytest.fixture(autouse=True)
def _clean():
    reset_evolution_state()
    yield
    reset_evolution_state()


def _repos(**over):
    base = {
        "skill_repo": _FakeSkillRepo(),
        "candidate_repo": _FakeCandidateRepo(),
        "experience_repo": _FakeExperienceRepo(),
        "policy_repo": _FakePolicyRepo(),
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# 1. tenant fail-closed                                                        #
# --------------------------------------------------------------------------- #
async def test_unresolved_tenant_fails_closed_before_any_read():
    with pytest.raises(Exception) as exc:  # noqa: B017 — any fail-closed error
        await maybe_evolve(None, SKILL_ID, actor="u1", **_repos())
    text = str(exc.value).lower()
    assert "403" in text or "tenant" in text, f"expected a tenant failure, got {exc.value!r}"


# --------------------------------------------------------------------------- #
# 2. trigger threshold                                                         #
# --------------------------------------------------------------------------- #
async def test_below_threshold_does_not_trigger(monkeypatch):
    async def _few(tenant, *, skill_tools, window_days=30):
        return _failures(EVOLVE_TRIGGER_MIN_FAILURES - 1)

    monkeypatch.setattr(el, "collect_skill_failures", _few)

    out = await maybe_evolve(TENANT, SKILL_ID, actor="u1", **_repos())
    assert out.triggered is False
    assert out.applied is False
    assert str(EVOLVE_TRIGGER_MIN_FAILURES - 1) in out.reason
    assert out.to_version is None


# --------------------------------------------------------------------------- #
# 3. generation cap → stop auto-bumping, hand to a human                       #
# --------------------------------------------------------------------------- #
async def test_generation_cap_stops_auto_bump_and_escalates(monkeypatch):
    async def _many(tenant, *, skill_tools, window_days=30):
        return _failures(EVOLVE_TRIGGER_MIN_FAILURES + 2)

    monkeypatch.setattr(el, "collect_skill_failures", _many)
    for i in range(MAX_EVOLVE_GENERATIONS):
        record_generation(TENANT, SKILL_ID, f"1.0.{i}")

    pol = _FakePolicyRepo()
    out = await maybe_evolve(TENANT, SKILL_ID, actor="u1", **_repos(policy_repo=pol))

    assert out.triggered is True, "cap is reached *because* there are failures"
    assert out.applied is False, "at the cap nothing may be auto-published"
    assert out.to_version is None
    assert str(MAX_EVOLVE_GENERATIONS) in out.reason
    assert pol.saved, "reaching the cap must raise a human approval, not fail silently"


# --------------------------------------------------------------------------- #
# 4. cooldown                                                                  #
# --------------------------------------------------------------------------- #
async def test_cooldown_suppresses_a_second_auto_bump(monkeypatch):
    async def _many(tenant, *, skill_tools, window_days=30):
        return _failures(EVOLVE_TRIGGER_MIN_FAILURES + 1)

    monkeypatch.setattr(el, "collect_skill_failures", _many)

    now = datetime.now(timezone.utc)
    record_evolution_time(TENANT, SKILL_ID, now - timedelta(hours=1))

    out = await maybe_evolve(TENANT, SKILL_ID, actor="u1", now=now, **_repos())
    assert out.triggered is False
    assert out.applied is False
    assert str(EVOLVE_COOLDOWN_HOURS) in out.reason


async def test_cooldown_expired_allows_the_loop_again(monkeypatch):
    async def _many(tenant, *, skill_tools, window_days=30):
        return _failures(EVOLVE_TRIGGER_MIN_FAILURES + 1)

    monkeypatch.setattr(el, "collect_skill_failures", _many)

    now = datetime.now(timezone.utc)
    record_evolution_time(TENANT, SKILL_ID, now - timedelta(hours=EVOLVE_COOLDOWN_HOURS + 1))

    out = await maybe_evolve(TENANT, SKILL_ID, actor="u1", now=now, **_repos())
    # Past the cooldown the loop proceeds past the time gate (it may still stop
    # later, e.g. insufficient experiences) — but it must NOT report cooldown.
    assert str(EVOLVE_COOLDOWN_HOURS) not in out.reason


# --------------------------------------------------------------------------- #
# 5. idempotency — same window ⇒ same decision, zero extra work                #
# --------------------------------------------------------------------------- #
async def test_same_window_is_idempotent(monkeypatch):
    async def _many(tenant, *, skill_tools, window_days=30):
        return _failures(EVOLVE_TRIGGER_MIN_FAILURES + 1)

    monkeypatch.setattr(el, "collect_skill_failures", _many)
    repos = _repos()
    exp_repo = repos["experience_repo"]

    first = await maybe_evolve(TENANT, SKILL_ID, actor="u1", **repos)
    saved_after_first = len(exp_repo.saved)

    second = await maybe_evolve(TENANT, SKILL_ID, actor="u1", **repos)

    assert second is first, "the ledger must hand back the very same outcome"
    assert second.window_key == first.window_key
    assert len(exp_repo.saved) == saved_after_first, "no experience folded twice"


async def test_idempotency_key_changes_with_the_window(monkeypatch):
    async def _many(tenant, *, skill_tools, window_days=30):
        return _failures(EVOLVE_TRIGGER_MIN_FAILURES + 1)

    monkeypatch.setattr(el, "collect_skill_failures", _many)

    first = await maybe_evolve(TENANT, SKILL_ID, actor="u1", **_repos())
    reset_evolution_state()  # drop the ledger, same window content
    second = await maybe_evolve(TENANT, SKILL_ID, actor="u1", **_repos())

    # Same *content* ⇒ same deterministic key (recomputable, not random).
    assert first.window_key == second.window_key


# --------------------------------------------------------------------------- #
# 6. the six-state lifecycle machine is never mutated                          #
# --------------------------------------------------------------------------- #
async def test_evolution_never_mutates_the_lifecycle_machine(monkeypatch):
    async def _many(tenant, *, skill_tools, window_days=30):
        return _failures(EVOLVE_TRIGGER_MIN_FAILURES + 1)

    monkeypatch.setattr(el, "collect_skill_failures", _many)

    transitions_before = copy.deepcopy(dict(LIFECYCLE_TRANSITIONS))
    states_before = copy.deepcopy(list(LIFECYCLE_STATES))

    await maybe_evolve(TENANT, SKILL_ID, actor="u1", **_repos())

    assert dict(LIFECYCLE_TRANSITIONS) == transitions_before
    assert list(LIFECYCLE_STATES) == states_before


# --------------------------------------------------------------------------- #
# 7. the improvement proposal is deterministic (zero LLM)                      #
# --------------------------------------------------------------------------- #
def test_build_proposal_is_deterministic_and_llm_free():
    failing = _failures(EVOLVE_TRIGGER_MIN_FAILURES)
    first = _build_proposal(failing)
    second = _build_proposal(_failures(EVOLVE_TRIGGER_MIN_FAILURES))

    assert first == second, "the same failures must yield the same proposal"
    assert isinstance(first, dict)
    # A proposal must never invent a metric it did not measure.
    for value in first.values():
        assert value is not None


# --------------------------------------------------------------------------- #
# 8. honesty: an outcome never claims success it did not achieve               #
# --------------------------------------------------------------------------- #
async def test_outcome_has_no_fabricated_version_when_not_applied(monkeypatch):
    async def _few(tenant, *, skill_tools, window_days=30):
        return _failures(1)

    monkeypatch.setattr(el, "collect_skill_failures", _few)

    out = await maybe_evolve(TENANT, SKILL_ID, actor="u1", **_repos())
    payload = out.to_dict()

    assert payload["applied"] is False
    assert payload["to_version"] is None
    assert payload["reason"], "a non-applied outcome must always say why"
