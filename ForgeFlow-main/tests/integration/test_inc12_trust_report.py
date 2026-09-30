"""INC12 A6 — 六问 witness / TRUST_REPORT 落盘 (QA2 / yan-guoguan).

与 ``tests/integration/test_inc12_trust_loop.py`` 的分工
-------------------------------------------------------
那个文件是"六问**断言**套件"（13 用例，另一名 QA 落地，我已独立复跑：
11 passed / 2 xfailed）。本文件是"六问**取证**套件"：每一问都把答案连同
**真值来源（符号锚点，非行号）**写进一份机器可读 witness，落盘到
``qa_tmp/inc12_a6_trust_report.json`` 与 ``.md``，供审计/复盘直接引用，
而不是靠人去读测试代码。

硬约束：本文件的每个断言只读 ``GET /runs/{run_id}`` 真实响应，不读测试自己
造的 Python 对象；每问都带反空转下限（非空、非占位、与"放进去的值"相等）。

Q5 的诚实口径：本仓没有 ``invocation_id``，``steps[].observation`` 是
invocation 的**拷贝**（orchestrator.py 中把 invocation 拷贝进 steps[].observation 处）。因此"证据⇄答案双向追溯"
只能靠重新推导 ``step_id`` 完成 —— 不是 GAP，但也不是防篡改，故记 PARTIAL。
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest
from fastapi import FastAPI
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

pytestmark = pytest.mark.asyncio

# --------------------------------------------------------------------------- #
# 场景常量                                                                    #
# --------------------------------------------------------------------------- #
TENANT = "t-inc12-a6"
ACTOR_USER = "u-inc12-a6"
ACTOR_ROLE = "admin"
INTENT = "汇总华东区上季度订单并生成可审计的证据报告"

#: 只放真实实现（stdlib / 平台内建）。刻意不掺 ``research.search``：开发态桩件的
#: 诚实性已由兄弟文件的 ``test_q2_development_stub_is_never_a_silent_mock`` 钉住，
#: 本文件不复述，也避免取证时被当前工作区的临时改动污染。
PLAN = ["docs.parse", "analysis.score", "report.render"]

HONEST_STATES = {"ok", "error", "unavailable", "refused", "skipped"}

_QA_TMP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "qa_tmp",
)
_JSON_PATH = os.path.join(_QA_TMP, "inc12_a6_trust_report.json")
_MD_PATH = os.path.join(_QA_TMP, "inc12_a6_trust_report.md")

#: 模块级 TRUST_REPORT。key = "q1".."q6"。
TRUST_REPORT: dict[str, dict] = {}


# --------------------------------------------------------------------------- #
# 只假造聊天模型；planner / 闸门 / executor / handler / store / router 全是真的 #
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
    """定价表里有价格的模型 id —— 否则账单恒为 $0.00，Q6 无法回答。"""

    def __init__(self, payloads: list[str], usage: tuple[int, int] = (10, 5)) -> None:
        self._payloads = list(payloads)
        self._usage = usage
        self._last = "{}"
        self.model = "gpt-4o-mini"
        self._llm_type = "fake"

    def bind(self, **kwargs):  # noqa: ANN003
        return _FakeBound(self)

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._next()

    def _next(self) -> AIMessage:
        if self._payloads:
            self._last = self._payloads.pop(0)
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


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    llm_planner.clear_caches()
    yield
    reset_run_store()
    llm_planner.clear_caches()


async def _drive(monkeypatch) -> dict:
    """真实跑一遍，再经真实 ``GET /runs/{id}`` 读回。返回 decode 后的 JSON。"""
    plan_json = json.dumps(
        {"steps": [{"tool": t, "note": f"执行 {t}"} for t in PLAN]}, ensure_ascii=False
    )
    reflect_json = '{"success": true, "score": 1.0, "summary": "完成", "reasons": ["ok"]}'
    fake = _FakeModel([plan_json, reflect_json])

    monkeypatch.setattr(
        orch,
        "get_settings",
        lambda: _SettingsProxy(get_settings(), mode="llm", provider="ollama"),
    )
    monkeypatch.setattr(
        orch,
        "_build_planner_models",
        lambda: (fake, fake, [{"slot": "strong", "class": "_FakeModel", "model": fake.model}]),
    )

    handle = await run_task(
        TaskCreate(intent=INTENT, workflow_type="generic"),
        RequestContext(tenant_id=TENANT, user_id=ACTOR_USER, role=ACTOR_ROLE),
        bus=_RecordingBus(),
    )
    record = get_run_store().get(handle.run_id)
    assert record is not None

    app = FastAPI()
    app.dependency_overrides[resolve_tenant] = lambda: record.tenant_id
    app.include_router(runs_router.router, prefix="/runs")
    response = TestClient(app).get(f"/runs/{handle.run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _emit(q: str, status: str, source: str, answer: dict, note: str = "") -> None:
    """把一问的答案写进 TRUST_REPORT 并立即落盘（幂等，可重复写）。"""
    TRUST_REPORT[q] = {
        "status": status,
        "truth_source": source,
        "answer": answer,
        "note": note,
    }
    os.makedirs(_QA_TMP, exist_ok=True)
    payload = {
        "schema": "inc12.a6.trust_report/v1",
        "backend": "memory",
        "questions": TRUST_REPORT,
    }
    with open(_JSON_PATH, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# =========================================================================== #
# Q1 — 谁发起的？                                                              #
# =========================================================================== #
async def test_q1_who_initiated_is_answerable(monkeypatch, force_memory_backend):
    body = await _drive(monkeypatch)

    assert body["actor_user_id"] == ACTOR_USER
    assert body["actor_role"] == ACTOR_ROLE
    assert body["tenant_id"] == TENANT

    invocations = body["tool_invocations"]
    assert invocations, "反空转：没有任何调用记录"
    for inv in invocations:
        assert inv["actor_user_id"] == ACTOR_USER
        assert inv["actor_role"] == ACTOR_ROLE
        assert inv["tenant_id"] == TENANT

    _emit(
        "q1",
        "PASS",
        "orchestrator.py::run_task 中 actor_user_id=ctx.user_id / actor_role=ctx.role 处 "
        "→ api/routers/runs.py::get_run（actor）；"
        "hub_schemas.py::RunDetailResponse.tenant_id → runs.py::get_run 中填 tenant_id 处",
        {
            "actor_user_id": body["actor_user_id"],
            "actor_role": body["actor_role"],
            "tenant_id": body["tenant_id"],
            "per_invocation_attributed": len(invocations),
        },
        "Q1 三元组（user_id / tenant_id / role）在 run detail 与每条 invocation 上都齐了；"
        "兄弟文件里那条 tenant_id 的 xfail 已由 225025c 补齐。",
    )


# =========================================================================== #
# Q2 — 它调用了什么？                                                          #
# =========================================================================== #
async def test_q2_what_was_called_is_answerable(monkeypatch, force_memory_backend):
    body = await _drive(monkeypatch)
    invocations = body["tool_invocations"]

    assert [inv["tool"] for inv in invocations] == PLAN
    for inv in invocations:
        assert inv["status"] in HONEST_STATES
        assert inv["executed"] is (inv["status"] in {"ok", "error"})
        assert inv["arguments_hash"] and len(inv["arguments_hash"]) == 64
        assert inv["step_id"].startswith(f"{body['run_id']}:0:")
        assert inv["provider"]

    hashes = [inv["arguments_hash"] for inv in invocations]
    assert len(set(hashes)) == len(hashes), f"arguments_hash 不是输入的函数: {hashes}"

    _emit(
        "q2",
        "PASS",
        "orchestrator.py::_record_invocation 中往 task.context 的 tool_invocations 追加"
        " → orchestrator.py::run_task 中把 tool_invocations 挂到 RunRecord"
        " → api/routers/runs.py::get_run",
        {
            "tools": [inv["tool"] for inv in invocations],
            "statuses": [inv["status"] for inv in invocations],
            "providers": [inv["provider"] for inv in invocations],
            "arguments_hash_distinct": len(set(hashes)),
        },
    )


# =========================================================================== #
# Q3 — 为什么允许调用？                                                        #
# =========================================================================== #
async def test_q3_why_allowed_is_answerable(monkeypatch, force_memory_backend):
    body = await _drive(monkeypatch)
    invocations = body["tool_invocations"]

    for inv in invocations:
        assert inv["policy_decision"] == "allow", (
            f"{inv['tool']} 以 policy_decision={inv['policy_decision']!r} 执行了"
        )

    allowed = set(body["llm"].get("allowed_tools") or [])
    assert allowed, "反空转：run 上没有 RBAC 白名单"
    assert set(PLAN) <= allowed

    _emit(
        "q3",
        "PASS",
        "orchestrator.py::_llm_executor 中 policy_decision=_policy_label(decision)"
        " + orchestrator.py::_policy_label → api/routers/runs.py::get_run",
        {
            "policy_decisions": sorted({inv["policy_decision"] for inv in invocations}),
            "allowed_tools_declared": len(allowed),
            "plan_subset_of_allowlist": set(PLAN) <= allowed,
        },
    )


# =========================================================================== #
# Q4 — 它实际拿到了什么？                                                      #
# =========================================================================== #
async def test_q4_what_returned_is_answerable(monkeypatch, force_memory_backend):
    body = await _drive(monkeypatch)
    invocations = body["tool_invocations"]

    rendered = [i for i in invocations if i["tool"] == "report.render"]
    assert rendered, "反空转：report.render 没出现"
    inv = rendered[0]
    assert inv["status"] == "ok"
    content = inv["payload"]["content"]
    assert INTENT in content or "运行报告" in content
    # 独立重算：result_ref 必须是所返回内容的摘要，而不是常量。
    assert inv["result_ref"] == _sha256(content)[:32]

    for item in invocations:
        if item["status"] == "ok":
            assert item["result_ref"] and item["summary"] and item["payload"]

    parsed = next(i for i in invocations if i["tool"] == "docs.parse")
    scored = next(i for i in invocations if i["tool"] == "analysis.score")

    _emit(
        "q4",
        "PASS",
        "tool_executor.py::ToolExecutor.execute 中 result_ref 兜底（无 result_ref ⇒ "
        "_sha256(_canonical(payload))[:32]）；report.render 自带摘要在 "
        "tool_handlers.py::report_render 中 result_ref = digest[:32] → api/routers/runs.py::get_run",
        {
            "report_render_result_ref": inv["result_ref"],
            "result_ref_recomputed_equal": inv["result_ref"] == _sha256(content)[:32],
            "docs_parse_sections": parsed["payload"]["section_count"],
            "analysis_score_formula": scored["payload"]["formula"],
        },
        "注意：report.render 的 result_ref 由 handler 自己算（tool_handlers.py::report_render 中 result_ref = digest[:32]），"
        "executor 的兜底分支在本 plan 下不会被走到 —— 反空转注入要打在 handler 侧。",
    )


# =========================================================================== #
# Q5 — 它为什么得出这个答案？                                                  #
# =========================================================================== #
async def test_q5_why_this_answer_is_answerable(monkeypatch, force_memory_backend):
    body = await _drive(monkeypatch)
    invocations = body["tool_invocations"]
    steps = body["steps"]

    assert len(steps) == len(invocations), "存在没有证据的步骤"

    by_index = {step["index"]: step for step in steps}
    for inv in invocations:
        head, attempt, index = inv["step_id"].rsplit(":", 2)
        assert head == body["run_id"] and attempt == "0"
        step = by_index[int(index)]
        assert step["tool"] == inv["tool"]
        assert step["status"] == inv["status"]
        assert step["observation"] == inv  # 答案 → 证据

    assert body["status"] == "completed" and body["outcome"]

    _emit(
        "q5",
        "PARTIAL",
        "orchestrator.py::_llm_executor 中 payload 的 observation = invocation 拷贝（steps[].observation）；"
        "step_id 约定 ``{run_id}:{attempt}:{index}`` 在 planning.py::PlanStep 构建处",
        {
            "steps": len(steps),
            "invocations": len(invocations),
            "join_by": "step_id 重新推导（{run_id}:{attempt}:{index}）",
            "has_invocation_id": False,
            "observation_is_copy": True,
        },
        "可双向追溯，但靠重新推导 step_id，不是靠稳定身份：导出的 observation 无 "
        "invocation_id / parent_invocation_id，防篡改无从谈起。与兄弟文件的 "
        "GAP-Q5 xfail 口径一致，等 INC13 的 Claim Verification。",
    )


# =========================================================================== #
# Q6 — 花了多少钱 / 出了什么错 / 能不能恢复？                                  #
# =========================================================================== #
async def test_q6_cost_errors_recovery_is_answerable(monkeypatch, force_memory_backend):
    body = await _drive(monkeypatch)

    assert body["runtime_mode"] == "llm"
    assert body["total_tokens"] > 0, "用了模型却报 0 token"
    assert body["total_cost_usd"] > 0, "花了 token 却报 $0.00"
    assert body["errors"] == []
    assert body["status"] == "completed"
    assert body["experience_id"] and body["completed_at"]
    assert len(body["tool_invocations"]) == len(PLAN)

    _emit(
        "q6",
        "PASS",
        "orchestrator.py::run_task 中 total_tokens / total_cost_usd 来自 CostTracker 的 summary"
        " → api/routers/runs.py::list_runs（暴露用量）",
        {
            "total_tokens": body["total_tokens"],
            "total_cost_usd": body["total_cost_usd"],
            "errors": body["errors"],
            "status": body["status"],
            "experience_id_present": bool(body["experience_id"]),
            "completed_at_present": bool(body["completed_at"]),
        },
        "失败/重规划的取证（attempt>0 的证据链、replan 归属）由兄弟文件 "
        "test_q6_failed_run_is_still_fully_auditable / "
        "test_q6_replan_recovery_is_attributed 覆盖，此处不重复。",
    )


# =========================================================================== #
# witness 完整性自检                                                          #
# =========================================================================== #
async def test_trust_report_witness_is_complete_and_non_vacuous(force_memory_backend):
    """witness 必须六问齐全且每问都有非空答案 —— 否则落盘就是一张空表。"""
    assert set(TRUST_REPORT) == {"q1", "q2", "q3", "q4", "q5", "q6"}, sorted(TRUST_REPORT)

    for q, entry in TRUST_REPORT.items():
        assert entry["status"] in {"PASS", "PARTIAL", "GAP"}, q
        assert entry["truth_source"], f"{q} 没有真值来源"
        assert entry["answer"], f"{q} 的答案是空的"

    lines = [
        "# INC12 A6 — 六问 witness（机器可读原件：inc12_a6_trust_report.json）",
        "",
        "| 问 | 结论 | 真值来源 | 关键答案 |",
        "| --- | --- | --- | --- |",
    ]
    for q in ("q1", "q2", "q3", "q4", "q5", "q6"):
        entry = TRUST_REPORT[q]
        lines.append(
            f"| {q} | {entry['status']} | {entry['truth_source']} | "
            f"`{json.dumps(entry['answer'], ensure_ascii=False)[:180]}` |"
        )
    lines.append("")
    for q in ("q1", "q2", "q3", "q4", "q5", "q6"):
        if TRUST_REPORT[q]["note"]:
            lines.append(f"- **{q}** {TRUST_REPORT[q]['note']}")
    with open(_MD_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    with open(_JSON_PATH, "rb") as fh:
        raw = fh.read()
    assert not raw.startswith(b"\xef\xbb\xbf"), "witness 不能带 BOM"
    assert json.loads(raw.decode("utf-8"))["questions"] == TRUST_REPORT
