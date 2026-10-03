"""INC46 T04 — the lightweight sandbox (design §1.4 / §2.4).

Pins three things:

1. ``run_tests``' **default** (declarative) output is unchanged — every spelling
   of the default yields the identical case runs, and the declarative path never
   swaps in a mock provider;
2. ``sandbox="restricted"`` runs the contract's tool plan under **mock**
   bindings (``kind="development"`` / ``provider="sandbox-mock"``) with **no
   production side effect**, and ``register_mock_bindings()`` never introduces a
   new tool id (orphan guard intact);
3. an over-privileged step (``DANGEROUS``) is fail-closed: ``verdict="error"``
   with the verbatim message, and ``error`` is never a ``pass``.

Runs on the memory profile — no PostgreSQL, no network, no LLM.
"""

from __future__ import annotations

import pytest

from forgeflow.runtime import tool_registry
from forgeflow.runtime.gate import PLATFORM_PLAN_TOOLS
from forgeflow.skills import tester as tester_mod
from forgeflow.skills.contracts import SkillContract, SkillTestCase
from forgeflow.skills.tester import SandboxMode, generate_tests, run_tests


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts from the real default bindings; mocks never leak out."""
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    yield
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()


def _contract(**overrides) -> SkillContract:
    base = dict(
        goal="分析客户流失",
        procedure=["拉取数据", "计算概率", "生成建议"],
        tools=["data.query", "analysis.score", "report.render"],
        inputs={"intent": "string"},
        outputs={"summary": "string"},
        verification=["输出非空"],
        risk_level="low",
    )
    base.update(overrides)
    return SkillContract(**base)


# --------------------------------------------------------------------------- #
# 1. register_mock_bindings() — mock provider, same ids, orphan guard intact    #
# --------------------------------------------------------------------------- #


def test_register_mock_bindings_marks_every_binding_as_a_mock():
    tool_registry.register_mock_bindings()
    for tool_id in sorted(tool_registry.known_ids()):
        binding = tool_registry.resolve(tool_id)
        assert binding is not None, tool_id
        assert binding.kind == "development", tool_id
        assert binding.provider == "sandbox-mock", tool_id


def test_register_mock_bindings_preserves_the_orphan_guard():
    before = set(tool_registry.known_ids())
    tool_registry.register_mock_bindings()
    after = set(tool_registry.known_ids())
    # No new id, none dropped.
    assert before == after
    # The §9-4 orphan guard still holds.
    assert set(PLATFORM_PLAN_TOOLS) == after
    assert after == before


def test_register_mock_bindings_is_idempotent():
    first = tool_registry.register_mock_bindings()
    second = tool_registry.register_mock_bindings()
    assert set(first) == set(second)
    assert all(b.kind == "development" for b in second.values())


def test_real_providers_are_restorable():
    original_provider = tool_registry.resolve("code.commit").provider
    tool_registry.register_mock_bindings()
    assert tool_registry.resolve("code.commit").provider == "sandbox-mock"
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    assert tool_registry.resolve("code.commit").provider == original_provider


@pytest.mark.asyncio
async def test_mock_binding_handler_is_side_effect_free():
    tool_registry.register_mock_bindings()
    binding = tool_registry.resolve("research.search")
    assert binding is not None
    result = await binding.handler({"q": "x"}, None)
    assert result["ok"] is True
    assert result["mock"] is True
    assert result["tool_id"] == "research.search"
    assert result["provider"] == "sandbox-mock"


# --------------------------------------------------------------------------- #
# 2. Default (declarative) sandbox — byte-for-byte unchanged                     #
# --------------------------------------------------------------------------- #


def test_default_declarative_output_is_identical_across_spellings():
    contract = _contract(verification=[])  # so a boundary case really fails
    cases = generate_tests(contract)

    baseline = [r.to_dict() for r in run_tests(contract, cases)]
    for spelling in (
        {"sandbox": SandboxMode.DECLARATIVE},
        {"sandbox": "declarative"},
        {"sandbox": "DECLARATIVE"},  # normalised
        {"sandbox": None},
        {"sandbox": "an-unknown-mode"},  # unknown ⇒ declarative default
    ):
        assert [r.to_dict() for r in run_tests(contract, cases, **spelling)] == baseline


def test_declarative_path_does_not_swap_in_a_mock_provider():
    contract = _contract()
    cases = generate_tests(contract)
    run_tests(contract, cases)  # default
    # The registry is untouched: data.query keeps its real default provider.
    binding = tool_registry.resolve("data.query")
    assert binding is not None
    assert binding.provider != "sandbox-mock"


# --------------------------------------------------------------------------- #
# 3. Restricted sandbox — mock execution, no side effects, fail-closed          #
# --------------------------------------------------------------------------- #


def test_restricted_plan_with_allowed_classes_passes_under_mocks():
    contract = _contract(tools=["research.search", "data.query", "document.edit"])
    cases = generate_tests(contract)
    runs = run_tests(contract, cases, sandbox=SandboxMode.RESTRICTED)
    assert len(runs) == len(cases)
    assert {r.verdict for r in runs} == {"pass"}
    # The detail records the *real* mock execution evidence.
    assert "真执行 3 步" in runs[0].detail
    assert "sandbox-mock" in runs[0].detail
    # ... and the real providers were restored afterwards (no leak).
    for tool_id in ("research.search", "data.query", "document.edit"):
        binding = tool_registry.resolve(tool_id)
        assert binding is not None and binding.provider != "sandbox-mock"


def test_restricted_really_invokes_the_mock_handler(monkeypatch):
    """Over-claim guard: the mock handler must be *actually invoked* per step."""
    calls = {"n": 0}
    real = tool_registry._make_mock_handler

    def _counting(tool_id):
        inner = real(tool_id)

        async def _handler(args, ctx):
            calls["n"] += 1
            return await inner(args, ctx)

        return _handler

    monkeypatch.setattr(tool_registry, "_make_mock_handler", _counting)

    contract = _contract(tools=["data.query", "document.edit"])
    cases = generate_tests(contract)
    runs = run_tests(contract, cases, sandbox="restricted")

    assert {r.verdict for r in runs} == {"pass"}
    # 2 plan steps invoked once per case ⇒ the coroutine really ran.
    assert calls["n"] == 2 * len(runs)
    assert calls["n"] > 0  # non-vacuous


def test_restricted_does_not_leak_mock_providers():
    """Regression nail: a restricted run must restore every real provider.

    Counterfactual: deleting the ``finally: restore_bindings(snapshot)`` in
    ``tester._run_tests_restricted`` makes this test go **red** (the providers
    stay ``sandbox-mock``). Verified by temporarily removing the restore.
    """

    def _providers() -> dict:
        return {
            tid: tool_registry.resolve(tid).provider for tid in tool_registry.known_ids()
        }

    before = _providers()
    assert before, "反空转：注册表为空"
    assert "sandbox-mock" not in set(before.values())

    contract = _contract(tools=["data.query", "document.edit", "research.search"])
    run_tests(contract, generate_tests(contract), sandbox="restricted")

    assert _providers() == before


def test_restricted_mock_that_raises_is_fail_closed_error(monkeypatch):
    def _raising(tool_id):
        async def _handler(args, ctx):
            raise RuntimeError(f"mock boom: {tool_id}")

        return _handler

    monkeypatch.setattr(tool_registry, "_make_mock_handler", _raising)

    contract = _contract(tools=["data.query"])
    cases = [SkillTestCase(id="c1", category="normal", input={}, assertion="input_keys_declared")]
    runs = run_tests(contract, cases, sandbox="restricted")

    assert runs[0].verdict == "error"
    assert "RuntimeError" in runs[0].detail


def test_restricted_mock_with_bad_result_is_fail_closed_error(monkeypatch):
    def _bad(tool_id):
        async def _handler(args, ctx):
            return {"ok": False, "reason": "nope"}

        return _handler

    monkeypatch.setattr(tool_registry, "_make_mock_handler", _bad)

    contract = _contract(tools=["data.query"])
    cases = [SkillTestCase(id="c1", category="normal", input={}, assertion="input_keys_declared")]
    runs = run_tests(contract, cases, sandbox="restricted")

    assert runs[0].verdict == "error"
    assert runs[0].verdict != "pass"


def test_restricted_empty_plan_is_fail_closed_error():
    contract = _contract(tools=[])
    cases = [SkillTestCase(id="c1", category="normal", input={}, assertion="input_keys_declared")]
    runs = run_tests(contract, cases, sandbox="restricted")
    assert runs[0].verdict == "error"


def test_restricted_over_privileged_tool_is_fail_closed_error():
    contract = _contract(tools=["data.query", "payment.transfer"])
    cases = generate_tests(contract)
    runs = run_tests(contract, cases, sandbox=SandboxMode.RESTRICTED)
    assert runs, "反空转：没有用例"
    for run in runs:
        assert run.verdict == "error"
        assert run.verdict != "pass"
        assert run.detail == "沙箱不允许 DANGEROUS 工具 'payment.transfer'，已按越权拦截"


def test_restricted_over_privileged_code_commit_is_blocked():
    contract = _contract(tools=["code.commit"])
    cases = [SkillTestCase(id="c1", category="normal", input={}, assertion="input_keys_declared")]
    runs = run_tests(contract, cases, sandbox="restricted")
    assert runs[0].verdict == "error"
    assert runs[0].detail == "沙箱不允许 DANGEROUS 工具 'code.commit'，已按越权拦截"


def test_restricted_error_is_never_counted_as_pass():
    contract = _contract(tools=["skill.publish"])
    cases = generate_tests(contract)
    runs = run_tests(contract, cases, sandbox="restricted")
    passed = sum(1 for r in runs if r.verdict == "pass")
    verified_denom = sum(1 for r in runs if r.verdict in ("pass", "fail"))
    assert passed == 0
    assert verified_denom == 0  # every case is an error ⇒ no verified denominator


def test_restricted_sandbox_preserves_the_orphan_guard():
    contract = _contract(tools=["document.edit"])
    cases = generate_tests(contract)
    run_tests(contract, cases, sandbox="restricted")
    assert set(PLATFORM_PLAN_TOOLS) == set(tool_registry.known_ids())


def test_sandbox_mode_constants():
    assert SandboxMode.DECLARATIVE == "declarative"
    assert SandboxMode.RESTRICTED == "restricted"
    assert tester_mod.RESTRICTED_ALLOWED_CLASSES == frozenset({"READ", "WRITE", "EXTERNAL"})
