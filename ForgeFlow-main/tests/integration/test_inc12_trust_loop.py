"""INC12 A4 — 六问信任闭环集成测试 (Trusted Agent Runtime).

Why this file exists
--------------------
``tests/unit/test_inc12_tool_executor.py`` pins the **executor contract** and
``tests/unit/test_inc12_llm_executor_real.py`` pins that invocations reach
``GET /runs/{id}``. Neither answers the question an enterprise buyer actually
asks. This file is the **acceptance suite for the closed loop**: every test
answers exactly one of the six questions and reads its answer back *through the
real API chain* — ``run_task`` (real planner → real RBAC gate → real
PolicyEngine → real ``ToolExecutor`` → real handler) → ``RunRecord`` →
``GET /runs/{run_id}`` (real router, real ``RunDetailResponse``, real tenant
scoping). Nothing here asserts on a value the test itself invented.

The six questions and the node id that answers each
---------------------------------------------------
===  ============================================  ==========================================
Q    问题                                           test node id
===  ============================================  ==========================================
  1    谁发起的？                                     ``test_q1_who_initiated_the_run``
                                                   ``test_q1_cross_tenant_read_is_404``
                                                   ``test_q1_tenant_id_is_on_the_run_detail``
2    它调用了什么？                                 ``test_q2_what_was_called_the_real_invocation_trail``
                                                   ``test_q2_development_stub_is_never_a_silent_mock``
3    为什么允许调用？                               ``test_q3_why_the_call_was_allowed``
                                                   ``test_q3_denied_tool_never_executes``
4    它实际拿到了什么？                             ``test_q4_what_it_actually_got_back``
5    它为什么得出这个答案？                         ``test_q5_evidence_and_answer_traceable_both_ways``
                                                   ``test_q5_observation_carries_parent_pointer`` *
6    花了多少钱/出了什么错/能不能恢复？             ``test_q6_cost_errors_and_recovery``
                                                   ``test_q6_failed_run_is_still_fully_auditable``
                                                   ``test_q6_replan_recovery_is_attributed``
===  ============================================  ==========================================

``*`` marks a test that is **xfail because production cannot answer the question
yet** — a real, declared GAP, not a pass. See ``docs`` evidence file
``9823331k-anti-vacuity-evidence.md`` for the red output that proves it.

Anti-vacuity discipline baked into the assertions
-------------------------------------------------
Every test in this file is built so a *vacuous* production value cannot satisfy
it. Concretely:

* before iterating evidence the test asserts the evidence list is **non-empty**
  and has the **exact expected length** — an empty ``[]`` can never satisfy a
  "for every invocation …" style assertion here;
* identity fields are asserted for **equality against the value that was put
  in**, never for mere presence (``assert inv["actor_user_id"] == ACTOR``, not
  ``assert inv["actor_user_id"]``);
* ``arguments_hash`` / ``result_ref`` are **re-derived independently in the
  test** (its own ``json.dumps`` + ``hashlib``), never imported from the module
  under test, so a change of the canonicalisation or a constant hash is caught;
* tenant scoping is pinned by a **negative** read (wrong tenant ⇒ 404), so a
  router that stopped scoping would go red.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

import forgeflow.runtime.orchestrator as orch
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.routers import runs as runs_router
from forgeflow.config import get_settings
from forgeflow.runtime import llm_planner
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    get_run_store,
    reset_run_store,
    run_task,
)
from forgeflow.runtime.tool_registry import UNBOUND_TOOLS

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Constants — the one scenario every "happy path" question is answered against #
# --------------------------------------------------------------------------- #
#: The tenant / user / role that *initiates* the run. Q1 reads these back.
TENANT = "t-inc12-a4"
ACTOR_USER = "u-inc12-a4"
ACTOR_ROLE = "admin"

#: A second tenant, used only for the negative (cross-tenant) read.
OTHER_TENANT = "t-inc12-a4-intruder"

INTENT = "汇总华东区上季度订单并生成可审计的证据报告"

#: The plan the (faked) Supervisor model returns. Every tool is a real, stdlib
#: or shipped implementation — and ``research.search`` is included on purpose so
#: the dev-stub honesty question (Q2) has a real subject.
PLAN = ["research.search", "docs.parse", "analysis.score", "report.render"]

#: The five honest states ``ToolExecutor`` may record (tool_executor.py:16-28).
HONEST_STATES = {"ok", "error", "unavailable", "refused", "skipped"}


# --------------------------------------------------------------------------- #
# Test doubles — ONLY the chat model is faked. The planner, the gates, the      #
# executor, the handlers, the run store and the router are all the real ones.   #
# --------------------------------------------------------------------------- #
class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


class _FakeBound:
    def __init__(self, model: "_FakeModel") -> None:
        self._model = model

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._model._next()


class _FakeModel:
    """Deterministic stand-in for the Supervisor chat model.

    It returns the *scripted* plan / reflection JSON and reports real-looking
    token usage, which is what lets Q6 assert a non-zero cost ledger without
    dialling a provider.
    """

    def __init__(self, payloads: list[str], usage: tuple[int, int] = (10, 5)) -> None:
        self._payloads = list(payloads)
        self._usage = usage
        self._last = "{}"
        self.calls: list[str] = []
        # A **priced** model id from ``CostTracker.MODEL_COSTS_PER_1K``. This is
        # deliberately not "fake-test-model": an id the price table does not
        # know is billed at $0.00 by design (cost_tracker.py:83), which would
        # make Q6's "花了多少钱？" unanswerable. Q6 needs the ledger to really
        # compute money, so the model must be one that has a rate.
        self.model = "gpt-4o-mini"
        self._llm_type = "fake"

    def bind(self, **kwargs):  # noqa: ANN003
        return _FakeBound(self)

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._next()

    def _next(self) -> AIMessage:
        if self._payloads:
            self._last = self._payloads.pop(0)
        self.calls.append(self._last)
        return AIMessage(
            content=self._last,
            usage_metadata={
                "input_tokens": self._usage[0],
                "output_tokens": self._usage[1],
                "total_tokens": sum(self._usage),
            },
        )


_UNSET = object()


class _SettingsProxy:
    def __init__(self, real, *, mode: object = _UNSET, provider: object = _UNSET) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_mode", mode)
        object.__setattr__(self, "_provider", provider)

    def __getattr__(self, item: str):
        if item == "agent_runtime_mode" and self._mode is not _UNSET:
            return self._mode
        if item == "llm_provider" and self._provider is not _UNSET:
            return self._provider
        return getattr(self._real, item)


def _patch_settings(monkeypatch, *, mode=None, provider=None) -> None:
    proxy = _SettingsProxy(
        get_settings(),
        mode=_UNSET if mode is None else mode,
        provider=_UNSET if provider is None else provider,
    )
    monkeypatch.setattr(orch, "get_settings", lambda: proxy)


def _patch_models(monkeypatch, fake: _FakeModel) -> None:
    monkeypatch.setattr(
        orch,
        "_build_planner_models",
        lambda: (fake, fake, [{"slot": "strong", "class": "_FakeModel", "model": fake.model}]),
    )


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    llm_planner.clear_caches()
    yield
    reset_run_store()
    llm_planner.clear_caches()


def _ctx(role: str = ACTOR_ROLE, user: str = ACTOR_USER, tenant: str = TENANT) -> RequestContext:
    return RequestContext(tenant_id=tenant, user_id=user, role=role)


def _client_for(tenant: str) -> TestClient:
    """A real FastAPI app mounting the real runs router, scoped to ``tenant``."""
    from fastapi import FastAPI

    app = FastAPI()
    app.dependency_overrides[resolve_tenant] = lambda: tenant
    app.include_router(runs_router.router, prefix="/runs")
    return TestClient(app)


def _canonical_json(value) -> str:
    """Independent canonical JSON — deliberately re-implemented, NOT imported.

    ``forgeflow.runtime.tool_executor._canonical`` is the production copy. If
    this test imported it, a change to the canonicalisation would silently
    change both sides and the hash assertion would stay green while the stored
    hash no longer matches what the docs claim it is.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _drive_llm_run(monkeypatch, *, plan=PLAN, intent=INTENT, ctx=None, reflect=None):
    """Run the real loop and read it back through the real ``GET /runs/{id}``.

    Returns ``(handle, record, body)``. ``body`` is the **decoded JSON the API
    returned** — every assertion in this file reads from it, never from an
    in-memory object the test built itself.
    """
    plan_json = json.dumps(
        {"steps": [{"tool": tool, "note": f"执行 {tool}"} for tool in plan]},
        ensure_ascii=False,
    )
    reflect_json = reflect or '{"success": true, "score": 1.0, "summary": "完成", "reasons": ["ok"]}'
    fake = _FakeModel([plan_json, reflect_json])

    _patch_settings(monkeypatch, mode="llm", provider="ollama")
    _patch_models(monkeypatch, fake)

    handle = await run_task(
        TaskCreate(intent=intent, workflow_type="generic"),
        ctx or _ctx(),
        bus=_RecordingBus(),
    )
    record = get_run_store().get(handle.run_id)
    assert record is not None, "run_task must persist a RunRecord"

    response = _client_for(record.tenant_id).get(f"/runs/{handle.run_id}")
    assert response.status_code == 200, response.text
    return handle, record, response.json()


def _invocations(body: dict) -> list[dict]:
    """The run's evidence list, with an anti-vacuity guard on its shape."""
    invocations = body["tool_invocations"]
    assert isinstance(invocations, list), "tool_invocations must be a list"
    assert invocations, "anti-vacuity: the run recorded no tool invocation at all"
    return invocations


# =========================================================================== #
# Q1 — 谁发起的？                                                              #
# =========================================================================== #
async def test_q1_who_initiated_the_run(monkeypatch, force_memory_backend):
    """Q1: ``GET /runs/{id}`` names who initiated the run — user_id + role.

    Truth source: ``orchestrator.run_task`` writes ``actor_user_id`` /
    ``actor_role`` onto the ``RunRecord`` (orchestrator.py:1070-1071) and
    ``api/routers/runs.py:97-98`` exposes them. The values asserted here are the
    ones handed to ``RequestContext`` — a run attributed to anyone else is a
    forged audit trail, so this is an **equality** assertion, not a presence
    check.
    """
    _handle, _record, body = await _drive_llm_run(monkeypatch)

    assert body["actor_user_id"] == ACTOR_USER
    assert body["actor_role"] == ACTOR_ROLE
    # The run's own identity, so an answer can never be about another run.
    assert body["intent"] == INTENT

    # …and the same answer is on **every single invocation**, so one exported
    # piece of evidence is attributable on its own (tool_executor.py:461-462).
    invocations = _invocations(body)
    for inv in invocations:
        assert inv["actor_user_id"] == ACTOR_USER
        assert inv["actor_role"] == ACTOR_ROLE


async def test_q1_cross_tenant_read_is_404(monkeypatch, force_memory_backend):
    """Q1 (scope): another tenant cannot even see the run → 404, never 200.

    Truth source: ``api/routers/runs.py:32-37`` — a cross-tenant lookup is
    "not found", not "forbidden", so the response never leaks existence.
    """
    _handle, record, body = await _drive_llm_run(monkeypatch)
    assert body["run_id"] == record.run_id

    intruder = _client_for(OTHER_TENANT)
    response = intruder.get(f"/runs/{record.run_id}")
    assert response.status_code == 404, response.text


async def test_q1_tenant_id_is_on_the_run_detail(monkeypatch, force_memory_backend):
    """Q1: the run detail names the tenant as well as the user + role.

    The acceptance question is "谁发起的？ → user_id / tenant_id / role".

    This test was originally written **xfail**: ``RunDetailResponse`` carried
    ``actor_user_id`` / ``actor_role`` but no ``tenant_id``, so an exported run
    record lost its tenancy (INC12 GAP-Q1). INC12 A5b added
    ``RunDetailResponse.tenant_id`` (api/hub_schemas.py:75) and the router now
    fills it (api/routers/runs.py:102), so the gap is closed and the xfail
    marker was removed — an xfail left in place here would be a test that can
    never fail again.
    """
    _handle, _record, body = await _drive_llm_run(monkeypatch)
    assert body["tenant_id"] == TENANT


# =========================================================================== #
# Q2 — 它调用了什么？                                                          #
# =========================================================================== #
async def test_q2_what_was_called_the_real_invocation_trail(monkeypatch, force_memory_backend):
    """Q2: the API names every tool the run really called, in order, with proof.

    Truth source: ``ToolExecutor.execute`` (tool_executor.py:223) is the only
    execution entry point; each call is appended to
    ``task.context["tool_invocations"]`` (orchestrator.py:89-98) and copied onto
    the run (orchestrator.py:1065). What is asserted per invocation:

    * ``tool`` — the exact plan tool id, in plan order (no dropped / invented
      step);
    * ``status`` ∈ the five honest states and ``executed`` agrees with it — the
      contract at tool_executor.py:16-28, which is what stops the old
      "everything is ok" fiction;
    * ``step_id`` / ``run_id`` / ``attempt`` — the call is pinned to this run;
    * ``provider`` — what actually backed the call;
    * ``latency_ms`` — a real, non-negative measurement.
    """
    _handle, _record, body = await _drive_llm_run(monkeypatch)
    invocations = _invocations(body)

    assert [inv["tool"] for inv in invocations] == PLAN
    assert len(invocations) == len(PLAN)

    for inv in invocations:
        assert inv["run_id"] == body["run_id"]
        assert inv["status"] in HONEST_STATES
        # The executed flag is the whole point of the honest-status contract:
        # ok/error ⇒ the handler really ran; everything else ⇒ it did not.
        assert inv["executed"] is (inv["status"] in {"ok", "error"})
        assert isinstance(inv["provider"], str) and inv["provider"]
        assert inv["latency_ms"] >= 0
        assert inv["attempt"] == 0
        # A step_id that does not start with the run's own id is not this run's
        # evidence — this is what makes the Q5 join trustworthy.
        assert inv["step_id"].startswith(f"{body['run_id']}:0:")

    # Nothing the platform deliberately left unbound may appear as executed.
    called = {inv["tool"] for inv in invocations}
    for tool in called & set(UNBOUND_TOOLS):
        matching = [i for i in invocations if i["tool"] == tool]
        assert all(i["executed"] is False for i in matching)


async def test_q2_development_stub_is_never_a_silent_mock(monkeypatch, force_memory_backend):
    """Q2 (honesty): a development stub is labelled as one, never passed off real.

    ``research.search`` has no Tavily key in the test profile, so its binding is
    ``kind="development"`` (tool_registry.py:130-136). The environment gate
    (tool_executor.py:264-288) either refuses it outside dev, or — in dev — lets
    it run and **must** stamp ``development_stub=True``. What must never happen
    is a silent ``ok`` with no stub flag: that is exactly the "looks real, is a
    mock" defect an enterprise audit hunts.
    """
    _handle, _record, body = await _drive_llm_run(monkeypatch)
    invocations = _invocations(body)

    research = [inv for inv in invocations if inv["tool"] == "research.search"]
    assert research, "anti-vacuity: research.search never appeared on the trail"
    inv = research[0]

    if inv["status"] == "refused":
        # The environment gate refused it — then it did NOT run, and says why.
        assert inv["executed"] is False
        assert inv["error"]
    else:
        assert inv["status"] == "ok"
        assert inv["executed"] is True
        # The load-bearing assertion: a dev stub that ran is flagged as one.
        assert inv["development_stub"] is True
        assert inv["provider"]


# =========================================================================== #
# Q3 — 为什么允许调用？                                                        #
# =========================================================================== #
async def test_q3_why_the_call_was_allowed(monkeypatch, force_memory_backend):
    """Q3: every call carries the real gate verdict that let it through.

    Truth source: ``orchestrator._llm_executor`` evaluates the PolicyEngine
    **before** the tool runs (orchestrator.py:454-461) and RBAC before that
    (orchestrator.py:444); the verdict is written onto the invocation verbatim
    via ``_policy_label`` (orchestrator.py:75-86). ``"not_evaluated"`` on a call
    that executed is the fiction this assertion exists to catch: it would mean
    the tool ran with no gate decision on record.
    """
    _handle, _record, body = await _drive_llm_run(monkeypatch)
    invocations = _invocations(body)

    for inv in invocations:
        assert inv["policy_decision"] == "allow", (
            f"{inv['tool']} executed with policy_decision={inv['policy_decision']!r}; "
            "a call with no recorded allow verdict is ungoverned"
        )

    # The RBAC allowlist the plan was constrained to is on the run too, so
    # "why was this tool in the plan at all?" is answerable from the API.
    allowed = set(body["llm"].get("allowed_tools") or [])
    assert allowed, "anti-vacuity: the run recorded no RBAC allowlist"
    assert set(PLAN) <= allowed


async def test_q3_denied_tool_never_executes(force_memory_backend):
    """Q3 (negative): a role without the grant gets 0 calls, not a silent one.

    ``viewer`` lacks ``execute:workflows`` (verified against
    ``rbac/policies.ROLE_PERMISSIONS``), and ``runtime/gate.check_tool_permission``
    is re-checked inside the executor **before** the tool runs
    (orchestrator.py:599-606). So the run must end with an explicit denial and
    with an **empty** invocation trail — the strongest possible evidence that
    nothing ran.
    """
    handle = await run_task(
        TaskCreate(intent=INTENT, workflow_type="generic"),
        _ctx(role="viewer", user="u-inc12-viewer"),
        bus=_RecordingBus(),
    )
    record = get_run_store().get(handle.run_id)
    response = _client_for(record.tenant_id).get(f"/runs/{handle.run_id}")
    assert response.status_code == 200, response.text
    body = response.json()

    # Anti-vacuity: a run that never reached the gate would also have [] here,
    # so pin that the gate *spoke* — an explicit, named denial is on the record.
    assert body["errors"], "the RBAC gate must say why it refused"
    assert any("权限不足" in err for err in body["errors"]), body["errors"]

    assert body["tool_invocations"] == []
    assert body["steps"] == []

    # …and the refused run is still attributed (Q1 holds on a denied run too).
    assert body["actor_user_id"] == "u-inc12-viewer"
    assert body["actor_role"] == "viewer"


# =========================================================================== #
# Q4 — 它实际拿到了什么？                                                      #
# =========================================================================== #
async def test_q4_what_it_actually_got_back(monkeypatch, force_memory_backend):
    """Q4: what each call really returned is on the record — and verifiable.

    Three independent re-derivations, all computed in *this* file (never
    imported from the module under test), so a stored value that stopped being
    true would go red:

    1. ``arguments_hash`` of the first call == ``sha256`` of the canonical JSON
       of the arguments the runtime can actually supply
       (``orchestrator._tool_args``), which for attempt 0 / step 0 is exactly
       ``{"intent":…, "observations": [], "query":…, "text":…}``.
    2. the four ``arguments_hash`` values are **pairwise distinct** — a constant
       or ignored-input hash cannot pass (the argument bag grows by one
       observation per step, so a real hash must differ every time);
    3. ``report.render``'s ``result_ref`` == ``sha256(content)[:32]`` recomputed
       from the stored ``payload["content"]`` (tool_handlers.py:571-576).
    """
    _handle, _record, body = await _drive_llm_run(monkeypatch)
    invocations = _invocations(body)

    # (1) independent re-derivation of the first call's arguments_hash.
    first = invocations[0]
    expected_args = {
        "text": INTENT,
        "intent": INTENT,
        "query": INTENT,
        "observations": [],
    }
    assert first["arguments_hash"] == _sha256(_canonical_json(expected_args))

    # (2) the hash is a real function of its input, not a constant.
    hashes = [inv["arguments_hash"] for inv in invocations]
    assert len(set(hashes)) == len(hashes), f"arguments_hash collided: {hashes}"

    # (3) result_ref is the digest of what was actually returned.
    rendered = [inv for inv in invocations if inv["tool"] == "report.render"]
    assert rendered, "anti-vacuity: report.render never appeared on the trail"
    inv = rendered[0]
    assert inv["status"] == "ok"
    assert inv["payload"], "an ok call must carry what it got back"
    content = inv["payload"]["content"]
    assert INTENT in content or "运行报告" in content
    assert inv["result_ref"] == _sha256(content)[:32]

    # Every successful call left both a result reference and a human summary;
    # a call that failed left a reason instead. Nothing is silently empty.
    for item in invocations:
        if item["status"] == "ok":
            assert item["result_ref"], f"{item['tool']} returned ok with no result_ref"
            assert item["summary"], f"{item['tool']} returned ok with no summary"
            assert item["payload"] not in (None, {}, [])
        else:
            assert item["error"] or item["summary"]

    # Q4 read back step-by-step: docs.parse really parsed, analysis.score
    # really scored the observations it was handed.
    parsed = next(i for i in invocations if i["tool"] == "docs.parse")
    assert parsed["payload"]["section_count"] >= 1
    scored = next(i for i in invocations if i["tool"] == "analysis.score")
    assert scored["payload"]["metrics"]["total"] >= 1
    assert scored["payload"]["formula"] == (
        "0.40*coverage + 0.40*success_rate + 0.20*evidence_density"
    )


# =========================================================================== #
# Q5 — 它为什么最后得出这个答案？（证据 ⇄ 答案 双向追溯）                      #
# =========================================================================== #
async def test_q5_evidence_and_answer_traceable_both_ways(monkeypatch, force_memory_backend):
    """Q5: the answer and its evidence can be walked in both directions.

    Direction **answer → evidence**: every ``step`` carries the observation that
    produced it (orchestrator.py:502-504), so from the run's conclusion you can
    walk down to the tool output behind each step.

    Direction **evidence → answer**: every ``tool_invocation`` carries a
    ``step_id`` of the form ``{run_id}:{attempt}:{index}``
    (orchestrator.py:490), so from a piece of evidence you can walk *up* to the
    plan step and therefore to the conclusion it fed.

    This test pins the join in both directions and requires it to be **exact**
    (full record equality), not a fuzzy tool-name match.
    """
    _handle, _record, body = await _drive_llm_run(monkeypatch)
    invocations = _invocations(body)
    steps = body["steps"]
    assert len(steps) == len(invocations), "a step without evidence is an unsupported answer"

    # evidence → answer: step_id must resolve inside THIS run to a real step.
    by_index = {step["index"]: step for step in steps}
    inv_by_index = {}
    for inv in invocations:
        head, attempt, index = inv["step_id"].rsplit(":", 2)
        assert head == body["run_id"], "step_id does not belong to this run"
        assert attempt == "0"
        inv_by_index[int(index)] = inv

    assert sorted(inv_by_index) == sorted(by_index), "evidence and steps do not line up"

    for index, inv in inv_by_index.items():
        step = by_index[index]
        assert step["tool"] == inv["tool"]
        assert step["status"] == inv["status"]
        # answer → evidence: the step's observation IS the invocation record.
        assert step["observation"] == inv

    # The conclusion itself is on the record and is derived from that evidence.
    assert body["status"] == "completed"
    assert body["outcome"]

    # A concrete, verifiable link: the report.render step rendered exactly the
    # observations that precede it on the trail — so the final artefact is a
    # function of recorded evidence, not of anything off-record.
    render_index = next(i for i, inv in inv_by_index.items() if inv["tool"] == "report.render")
    prior = [inv for i, inv in sorted(inv_by_index.items()) if i < render_index]
    assert inv_by_index[render_index]["payload"]["observation_count"] == len(prior)


@pytest.mark.xfail(
    strict=False,
    reason=(
        "INC12 GAP-Q5: ToolInvocation has no stable identity (no invocation_id) "
        "and steps[].observation is a full COPY of the invocation "
        "(orchestrator.py:501-504) with no parent pointer back to the "
        "tool_invocations entry, so an exported observation cannot be joined to "
        "its source record except by re-deriving step_id."
    ),
)
async def test_q5_observation_carries_parent_pointer(monkeypatch, force_memory_backend):
    """Q5 (GAP): an observation must point at the invocation it came from.

    Today ``payload["observation"] = observation`` stores a *copy* of the
    invocation dict. Once a run is exported, archived or replayed there is no
    key on the observation that names the ``tool_invocations`` entry it was
    copied from — and no ``invocation_id`` on that entry to be pointed at. A
    copy with no identity makes tamper-evidence impossible: you cannot tell a
    rewritten observation from the original.
    """
    _handle, _record, body = await _drive_llm_run(monkeypatch)
    invocations = _invocations(body)

    ids = [inv.get("invocation_id") for inv in invocations]
    assert all(isinstance(i, str) and i for i in ids), "invocations have no stable identity"
    assert len(set(ids)) == len(ids), "invocation ids are not unique"

    for step in body["steps"]:
        pointer = step["observation"].get("parent_invocation_id") or step["observation"].get(
            "invocation_id"
        )
        assert pointer in ids, "an observation does not point back at its invocation"


# =========================================================================== #
# Q6 — 花了多少钱 / 出了什么错 / 能不能恢复？                                  #
# =========================================================================== #
async def test_q6_cost_errors_and_recovery(monkeypatch, force_memory_backend):
    """Q6: the ledger is real, the errors are honest, and the run is recoverable.

    Truth source: ``run_task`` fills ``total_tokens`` / ``total_cost_usd`` from
    the run's real usage via ``CostTracker`` (orchestrator.py:1045-1047) and the
    router exposes them (runs.py:82-83).

    The load-bearing invariant is **no phantom cost**: a cost figure may only be
    reported for a run that also reported tokens. A ledger that is non-zero with
    zero tokens is a fabricated number — the exact failure mode the cost work
    exists to prevent.
    """
    _handle, _record, body = await _drive_llm_run(monkeypatch)

    assert body["runtime_mode"] == "llm"  # the model really was used
    assert body["total_tokens"] > 0, "a run that used a model must report real tokens"
    # The ledger really bills: a priced model (see _FakeModel.model) times real
    # token usage must produce a positive cost. A $0.00 ledger on a run that
    # spent 30 tokens is the silent under-report Q6 exists to catch.
    assert body["total_cost_usd"] > 0, "tokens were spent but the ledger billed $0.00"
    assert body["total_tokens"] > 0

    # Nothing went wrong, and the run says so — an empty errors list must mean
    # empty, not "never populated".
    assert body["errors"] == []
    assert body["status"] == "completed"

    # Recoverability: the run left a durable handle (an Experience) and a
    # completion timestamp, so an operator can find and replay it.
    assert body["experience_id"]
    assert body["completed_at"]

    # The trail survives on the record — replay starts from real evidence.
    assert len(body["tool_invocations"]) == len(PLAN)
    assert body["loop"] == {} or isinstance(body["loop"], dict)


async def test_q6_failed_run_is_still_fully_auditable(force_memory_backend):
    """Q6 (failure): a failed run keeps its full evidence and says what broke.

    ``context={"simulate_failure": True}`` makes the deterministic executor
    append a real downstream failure (orchestrator.py:586, 683-686), which
    drives the run through replan to a failed terminal state. The question is
    whether an operator can still answer Q1-Q5 afterwards — they can, because
    every replan round leaves its own invocations on the trail.
    """
    handle = await run_task(
        TaskCreate(intent="离线跑一遍并模拟下游失败", workflow_type="generic",
                   context={"simulate_failure": True}),
        _ctx(),
        bus=_RecordingBus(),
    )
    record = get_run_store().get(handle.run_id)
    response = _client_for(record.tenant_id).get(f"/runs/{handle.run_id}")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "failed"
    assert body["errors"], "a failed run must say what failed"
    assert any("模拟失败" in err for err in body["errors"]), body["errors"]

    # Every replan round left evidence — not just the last one.
    attempts = {inv["attempt"] for inv in body["tool_invocations"]}
    assert {0, 1} <= attempts, f"only attempt(s) {attempts} left evidence"
    assert len(body["tool_invocations"]) >= 6

    # …and the actor is still named, so a failure is attributable too.
    assert body["actor_user_id"] == ACTOR_USER
    assert body["actor_role"] == ACTOR_ROLE


async def test_q6_replan_recovery_is_attributed(force_memory_backend):
    """Q6 (recovery): replaying a run through the API re-attributes the actor.

    ``POST /runs/{id}/replan`` (runs.py:123-136) is the real recovery path: it
    re-runs the intent as a **new** run. The new run must carry the *replaying*
    user's identity, not the original's — otherwise a replay launders
    attribution, and "谁发起的？" silently answers the wrong person.
    """
    handle = await run_task(
        TaskCreate(intent=INTENT, workflow_type="generic"),
        _ctx(),
        bus=_RecordingBus(),
    )
    original = get_run_store().get(handle.run_id)
    assert original is not None

    from fastapi import FastAPI

    from forgeflow.api.dependencies import get_current_user
    from forgeflow.rbac.models import UserContext

    app = FastAPI()
    app.dependency_overrides[resolve_tenant] = lambda: TENANT
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="u-inc12-recoverer", role=ACTOR_ROLE
    )
    app.include_router(runs_router.router, prefix="/runs")
    client = TestClient(app)

    response = client.post(
        f"/runs/{original.run_id}/replan", json={"reason": "QA: recover the failed run"}
    )
    assert response.status_code == 200, response.text
    new_run_id = response.json()["run_id"]
    assert new_run_id != original.run_id

    detail = client.get(f"/runs/{new_run_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()

    # The replay is attributed to who replayed it, and it really ran (evidence).
    assert body["actor_user_id"] == "u-inc12-recoverer"
    assert body["intent"] == INTENT
    assert body["tool_invocations"], "a replay produced no evidence"
    for inv in body["tool_invocations"]:
        assert inv["actor_user_id"] == "u-inc12-recoverer"
        assert inv["run_id"] == new_run_id
