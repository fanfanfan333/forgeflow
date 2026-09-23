"""PolicyEngine — RBAC → ABAC → risk → HITL (docs §6.3 / P0-09)."""

from __future__ import annotations

import uuid

import pytest

from forgeflow.governance.models import PolicyRecord
from forgeflow.governance.policy_engine import (
    PolicyEngine,
    classify_risk,
    evaluate_task_entry,
)
from forgeflow.repositories.memory.policy_repo import MemoryPolicyRepository

pytestmark = pytest.mark.asyncio


def _tenant() -> str:
    return f"t-pol-{uuid.uuid4().hex[:8]}"


def test_classify_risk_levels():
    assert classify_risk("transfer", "execute") == "high"
    assert classify_risk("skills", "delete") == "high"
    assert classify_risk("email", "send") == "high"
    assert classify_risk("skills", "write") == "medium"
    assert classify_risk("metrics", "read") == "low"


async def test_high_risk_requires_approval_and_creates_approval():
    tenant = _tenant()
    repo = MemoryPolicyRepository()
    engine = PolicyEngine(repo=repo)

    decision = await engine.evaluate(
        subject="admin", resource="transfer", action="execute",
        context={"role": "admin"}, tenant_id=tenant,
    )

    assert decision.allowed is True
    assert decision.risk_level == "high"
    assert decision.requires_approval is True
    assert decision.approval_id is not None
    approvals = await repo.list_approvals(tenant, status="pending")
    assert len(approvals) == 1
    assert approvals[0].risk_level == "high"


async def test_rbac_denies_role_without_permission():
    tenant = _tenant()
    engine = PolicyEngine(repo=MemoryPolicyRepository())

    decision = await engine.evaluate(
        subject="viewer", resource="workflows", action="execute",
        context={"role": "viewer"}, tenant_id=tenant,
    )

    assert decision.allowed is False
    assert "cannot" in decision.reason


async def test_unknown_role_denied():
    decision = await PolicyEngine(repo=MemoryPolicyRepository()).evaluate(
        subject="x", resource="workflows", action="execute", context={}, tenant_id="t"
    )
    assert decision.allowed is False


async def test_abac_deny_policy_short_circuits():
    tenant = _tenant()
    repo = MemoryPolicyRepository()
    await repo.save_policy(
        PolicyRecord(
            tenant_id=tenant, subject="*", resource="skills", action="approve",
            effect="deny", description="审批权已冻结",
        )
    )
    engine = PolicyEngine(repo=repo)

    decision = await engine.evaluate(
        subject="manager-1", resource="skills", action="approve",
        context={"role": "manager"}, tenant_id=tenant,
    )

    assert decision.allowed is False
    assert decision.hit_policy_id is not None


# --------------------------------------------------------------------------- #
# V6 regression — the entry gate must actually risk-grade, and the runtime must
# gate in-run tool calls (previously the engine was never invoked by runtime).
# --------------------------------------------------------------------------- #


async def test_sensitive_intent_escalates_at_entry():
    tenant = _tenant()
    decision = await evaluate_task_entry(
        "admin-1", "admin", "给供应商转账 10000 元", tenant_id=tenant
    )
    assert decision.allowed is True
    assert decision.risk_level == "high"
    assert decision.requires_approval is True


async def test_benign_intent_stays_low():
    tenant = _tenant()
    decision = await evaluate_task_entry(
        "admin-1", "admin", "分析华东地区销售数据并生成报告", tenant_id=tenant
    )
    assert decision.allowed is True
    assert decision.risk_level == "low"
    assert decision.requires_approval is False


async def test_sensitive_context_flag_forces_high():
    assert classify_risk("workflows", "execute", {"sensitive": True}) == "high"


async def test_tool_call_gate_blocks_high_risk():
    tenant = _tenant()
    repo = MemoryPolicyRepository()
    engine = PolicyEngine(repo=repo)

    decision = await engine.evaluate_tool_call(
        "admin-1", "admin", "payment.transfer", tenant_id=tenant, run_id="run-1"
    )

    assert decision.risk_level == "high"
    assert decision.requires_approval is True
    assert decision.approval_id is not None
    approvals = await repo.list_approvals(tenant, status="pending")
    assert len(approvals) == 1
    assert "transfer" in approvals[0].requested_action


async def test_tool_call_gate_allows_low_risk():
    tenant = _tenant()
    engine = PolicyEngine(repo=MemoryPolicyRepository())

    decision = await engine.evaluate_tool_call(
        "admin-1", "admin", "research.search", tenant_id=tenant, run_id="run-2"
    )

    assert decision.risk_level == "low"
    assert decision.requires_approval is False


async def test_tool_call_gate_blocks_outbound_send():
    tenant = _tenant()
    engine = PolicyEngine(repo=MemoryPolicyRepository())
    decision = await engine.evaluate_tool_call(
        "admin-1", "admin", "email.send", tenant_id=tenant, run_id="run-3"
    )
    assert decision.risk_level == "high"
    assert decision.requires_approval is True
